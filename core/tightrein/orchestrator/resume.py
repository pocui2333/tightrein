"""按状态续跑(architecture/09 3.4，design 15.10)：对象识别、find、next 与 continue。

进度就是状态：每一步先读对象当前的状态，按 domain/next_step.py 的映射表取下一步，只在能自动继续时调用模块；
模块只处理这一个对象，结果为 blocked 或 failed 时停止并带回原因。模块调用由编排层以 execute 提供，本模块不 import
任何 service。终端中遇到待确认操作时经 confirm 当场确认，同意并执行后继续；非交互时停在关口。
--from 重来：triage 对关联问题执行 retriage 后改为续跑这些问题；fix、verify 以 Issue 状态机的 restart 退回待修或
进行中的合并前验证，并把下游交接文档标记过期(文件保留)。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain import ids
from tightrein.domain.clock import Clock, format_iso, local_date
from tightrein.domain.enums import (
    Continuation,
    HandoffStatus,
    IssueEvent,
    IssueStatus,
    Probe,
    ProblemStatus,
    RunStage,
    Stage,
    VerifyPhase,
)
from tightrein.domain.issue import Issue, IssueContext
from tightrein.domain.next_step import NextStep, ProblemProgress, next_step
from tightrein.orchestrator import breaker
from tightrein.pipeline.issue.steps import transitions
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.pipeline.triage.steps.select import retriage_requests
from tightrein.store import locks
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.locks import LockHeld
from tightrein.store.repos import handoffs, issues, problems, signals, triage

PROBLEM = "problem"
ISSUE = "issue"
ORDER = (Stage.AGGREGATE, Stage.TRIAGE, Stage.ISSUE, Stage.FIX, Stage.VERIFY, Stage.RELEASE)
FROM_STAGES = (Stage.TRIAGE, Stage.FIX, Stage.VERIFY)
ACTOR = "loop"

PENDING_OPERATION = "pending-operation"
ISSUE_APPROVAL = "issue-approval"
FIX_PLAN = "fix-plan"
INTERACTIVE_FIX = "interactive-fix"
PR_REVIEW = "pr-review"
MANUAL_QUEUE = "manual-queue"
CANDIDATE_CHOICE = "candidate-choice"
AWAITING_DEPLOY = "awaiting-deploy"
BREAKER = "breaker"

NO_NEXT = "无后续步骤"
REACHED = "已到终点"
UNCHANGED = "这一步没有改变对象的状态"
PROBLEM_ID = re.compile(r"^P-\d{4,}$")
ISSUE_NUMBER = re.compile(r"^\d+$")
USER_GATES = {"issue approve": ISSUE_APPROVAL, "triage queue": MANUAL_QUEUE}


@dataclass(frozen=True)
class SubjectRef:
    kind: str
    id: str

    def to_dict(self) -> dict[str, str]:
        return {"type": self.kind, "id": self.id}


def identify(conn: sqlite3.Connection, token: str) -> SubjectRef:
    """编号：`P-0042` 为问题，`7` 或 `0007` 为 Issue；不存在时抛出 LookupError，写法不对时抛出 ValueError。"""
    text = token.strip()
    if PROBLEM_ID.match(text):
        if problems.get(conn, text) is None:
            raise LookupError(f"没有问题 {text}")
        return SubjectRef(PROBLEM, text)
    if ISSUE_NUMBER.match(text):
        issue_id = ids.issue_id(int(text))
        if issues.get(conn, issue_id) is None:
            raise LookupError(f"没有 Issue {issue_id}")
        return SubjectRef(ISSUE, issue_id)
    raise ValueError(f"无法识别的对象编号：{token}；问题写 P-0042，Issue 写 7 或 0007")


def _statuses(text: str) -> tuple[set[ProblemStatus], set[IssueStatus]]:
    found_problems: set[ProblemStatus] = set()
    found_issues: set[IssueStatus] = set()
    for part in text.split(","):
        name = part.strip()
        problem = next((item for item in ProblemStatus if name in (item.value, item.label)), None)
        issue = next((item for item in IssueStatus if name in (item.value, item.label)), None)
        if problem is None and issue is None:
            raise ValueError(f"没有状态 {name}")
        found_problems |= {problem} if problem else set()
        found_issues |= {issue} if issue else set()
    return found_problems, found_issues


def select(conn: sqlite3.Connection, selectors: Sequence[str]) -> list[SubjectRef]:
    """15.3 的选择器：编号、`status:<取值或中文名>`(可逗号分隔)、`run:<运行编号>`、`probe:<探针>`。"""
    found: list[SubjectRef] = []
    for selector in selectors:
        kind, _, argument = selector.partition(":")
        if not argument:
            found.append(identify(conn, selector))
        elif kind == "status":
            wanted_problems, wanted_issues = _statuses(argument)
            found += [SubjectRef(PROBLEM, item.id) for item in problems.find(conn, statuses=wanted_problems)
                      if wanted_problems]
            found += [SubjectRef(ISSUE, record.issue.id) for record in issues.find(conn)
                      if record.issue.status in wanted_issues]
        elif kind == "run":
            produced = {signal.id for signal in signals.find(conn, run_id=argument)}
            found += [SubjectRef(PROBLEM, item.id) for item in problems.find(conn)
                      if produced & set(problems.signal_ids(conn, item.id))]
        elif kind == "probe":
            found += [SubjectRef(PROBLEM, item.id) for item in problems.find(conn, probe=Probe(argument))]
        else:
            raise ValueError(f"无法识别的选择器：{selector}")
    return list(dict.fromkeys(found))


# 当前状态与下一步

def progress(conn: sqlite3.Connection, ref: SubjectRef) -> ProblemProgress | Issue:
    if ref.kind == ISSUE:
        return issues.get(conn, ref.id).issue
    problem = problems.get(conn, ref.id)
    record = triage.latest(conn, problem.id)
    issue = issues.get(conn, problem.issue_id).issue if problem.issue_id else None
    return ProblemProgress(problem.id, problem.status, record.result.disposition if record else None,
                           bool(retriage_requests(conn, problem.id)), issue)


PROBLEM_COMMANDS = ("aggregate", "triage", "triage queue", "issue create")


def command_for(target: SubjectRef, step: NextStep) -> str | None:
    if step.command is None:
        return None
    if step.command in ("aggregate", "triage queue"):
        return f"tightrein {step.command}"
    if step.command in PROBLEM_COMMANDS:
        return f"tightrein {step.command} --select {target.id}"
    return f"tightrein {step.command} {target.id.lstrip('0') or '0'}"


@dataclass(frozen=True)
class NextView:
    """target 为下一步实际处理的对象：已有 Issue 的问题跟随 Issue 的下一步时为该 Issue。"""

    ref: SubjectRef
    target: SubjectRef
    status: str
    status_label: str
    step: NextStep
    command: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.ref.to_dict(), "target": self.target.to_dict(), "status": self.status,
                "statusLabel": self.status_label, "module": self.step.module.value if self.step.module else None,
                "command": self.command, "canContinue": self.step.can_continue,
                "continuation": self.step.continuation.value, "gate": gate_of(self.status, self.step),
                "reason": self.step.reason or None}


def gate_of(status: str, step: NextStep) -> str | None:
    """不能自动继续时的关口类型；没有对应类型(无后续步骤、环境问题、转人工)时为空。"""
    if step.continuation is Continuation.INTERACTIVE:
        return INTERACTIVE_FIX
    if step.continuation is Continuation.WAIT_DEPLOY:
        return AWAITING_DEPLOY
    if step.continuation is not Continuation.USER:
        return None
    if status == IssueStatus.PENDING_MERGE.value and step.command is None:
        return PR_REVIEW
    return USER_GATES.get(step.command or "")


def next_view(conn: sqlite3.Connection, ref: SubjectRef) -> NextView:
    current = progress(conn, ref)
    step = next_step(current)
    target = ref
    status = current.status
    if isinstance(current, ProblemProgress) and current.issue is not None and step.command is not None \
            and step.command not in PROBLEM_COMMANDS:
        target = SubjectRef(ISSUE, current.issue.id)
        status = current.issue.status
    elif isinstance(current, ProblemProgress) and current.issue is not None and step.command is None \
            and step.continuation is not Continuation.NONE:
        status = current.issue.status
    return NextView(ref, target, status.value, status.label, step, command_for(target, step))


# find

@dataclass(frozen=True)
class Candidate:
    ref: SubjectRef
    title: str
    status: str
    matched: tuple[str, ...]
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.ref.to_dict(), "title": self.title, "status": self.status,
                "matched": list(self.matched), "at": format_iso(self.at)}


def _in_range(at: datetime, since: date | None, until: date | None, zone: tzinfo | None) -> bool:
    day = local_date(at, zone)
    return (since is None or day >= since) and (until is None or day <= until)


def find(conn: sqlite3.Connection, text: str, *, since: date | None = None, until: date | None = None,
         kind: str | None = None, limit: int, zone: tzinfo | None = None) -> list[Candidate]:
    """在问题标题与位置、Issue 标题与根因位置中做子串匹配(不区分大小写)，按最近时间排序；只返回候选，不选择。"""
    needle = text.casefold()
    found: list[Candidate] = []
    if kind in (None, PROBLEM):
        for problem in problems.find(conn):
            if problem.merged_into is not None or not _in_range(problem.last_seen_at, since, until, zone):
                continue
            locations = {signal.location for signal in signals.get_many(conn, problems.signal_ids(conn, problem.id))}
            matched = [name for name, value in (("title", problem.title),
                                                *(("location", item) for item in sorted(locations)))
                       if needle in value.casefold()]
            if matched:
                found.append(Candidate(SubjectRef(PROBLEM, problem.id), problem.title, problem.status.value,
                                       tuple(dict.fromkeys(matched)), problem.last_seen_at))
    if kind in (None, ISSUE):
        for record in issues.find(conn):
            issue = record.issue
            if not _in_range(issue.updated_at, since, until, zone):
                continue
            matched = [name for name, value in (("title", issue.title),
                                                *(("rootCause", item) for item in issue.root_cause))
                       if needle in value.casefold()]
            if matched:
                found.append(Candidate(SubjectRef(ISSUE, issue.id), issue.title, issue.status.value,
                                       tuple(dict.fromkeys(matched)), issue.updated_at))
    return sorted(found, key=lambda item: item.at, reverse=True)[:limit]


# continue

@dataclass(frozen=True)
class StepResult:
    """一次模块调用的结果；operation 为生成的待确认操作，gate 为模块给出的关口类型。"""

    status: HandoffStatus
    message: str
    operation: str | None = None
    gate: str | None = None


@dataclass(frozen=True)
class Stop:
    ref: SubjectRef
    status: str
    reason: str
    gate: str | None = None
    command: str | None = None
    operation: str | None = None
    failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.ref.to_dict(), "status": self.status, "reason": self.reason, "gate": self.gate,
                "next": self.command, "operation": self.operation, "failed": self.failed}


@dataclass(frozen=True)
class Progress:
    ref: SubjectRef
    command: str
    message: str


@dataclass
class ContinueReport:
    progress: list[Progress] = field(default_factory=list)
    stops: list[Stop] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(stop.failed for stop in self.stops)

    @property
    def gates(self) -> list[Stop]:
        return [stop for stop in self.stops if stop.gate is not None]


Execute = Callable[[SubjectRef, NextStep], StepResult]
Confirm = Callable[[str], bool]
Observe = Callable[[str, str, str, str], "str | None"]  # 熔断(orchestrator/breaker.py 的 Breaker.observe)
Stopping = Callable[[], "str | None"]  # 暂停或预算到达时返回原因，不再开始下一个对象


def _beyond(module: Stage | None, until: Stage | None) -> bool:
    return until is not None and module is not None and module in ORDER and ORDER.index(module) > ORDER.index(until)


class Resumer:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, execute: Execute, *, lock_ttl: timedelta,
                 until: Stage | None = None, interactive: bool = False, confirm: Confirm | None = None,
                 unattended: bool = False, observe: Observe | None = None, stopping: Stopping | None = None) -> None:
        self.conn = conn
        self.clock = clock
        self.execute = execute
        self.lock_ttl = lock_ttl
        self.until = until
        self.interactive = interactive
        self.confirm = confirm
        self.unattended = unattended  # gates.fix-session 为 auto：修复不需要交互会话，由 execute 以无人值守方式执行
        self.observe = observe
        self.stopping = stopping

    def continue_(self, refs: Sequence[SubjectRef]) -> ContinueReport:
        report = ContinueReport()
        for ref in refs:
            halted = self.stopping() if self.stopping is not None else None
            if halted is not None:
                report.skipped.append((ref.id, halted))
                continue
            try:
                with locks.held(self.conn, ref.id, self.clock, self.lock_ttl):
                    report.stops.append(self._one(ref, report))
            except LockHeld as error:
                report.skipped.append((ref.id, str(error)))
        return report

    def _stop(self, ref: SubjectRef, reason: str, **values: Any) -> Stop:
        view = next_view(self.conn, ref)
        values.setdefault("command", view.command)
        return Stop(ref, view.status, reason, **values)

    def _one(self, ref: SubjectRef, report: ContinueReport) -> Stop:
        while True:
            view = next_view(self.conn, ref)
            step = view.step
            if step.continuation is Continuation.NONE:
                return self._stop(ref, NO_NEXT, command=None)
            if _beyond(step.module, self.until):
                return self._stop(ref, REACHED)
            if step.continuation is Continuation.USER:
                return self._stop(ref, step.reason or "等待用户", gate=gate_of(view.status, step))
            if step.continuation is Continuation.INTERACTIVE and not (self.interactive or self.unattended):
                return self._stop(ref, "修复需要交互会话：在终端执行 fix start，或在当前会话中执行 fix start --here",
                                  gate=INTERACTIVE_FIX)
            if step.continuation is Continuation.WAIT_DEPLOY and ref.kind == PROBLEM:
                return self._stop(ref, "等待部署后由 aggregate 判定已解决", gate=AWAITING_DEPLOY)
            before = (view.status, view.command)
            result = self.execute(view.target, step)
            while result.status is HandoffStatus.BLOCKED and result.operation and self.confirm is not None \
                    and self.confirm(result.operation):
                report.progress.append(Progress(ref, view.command or "", f"已确认并执行 {result.operation}"))
                result = StepResult(HandoffStatus.OK, result.message)
            command = view.command or ""
            if result.status is HandoffStatus.FAILED:
                tripped = self._observe(ref, command, breaker.FAILED, result.message)
                return self._stop(ref, tripped or result.message, failed=True)
            if result.status is HandoffStatus.BLOCKED:
                gate = result.gate or (PENDING_OPERATION if result.operation else None)
                if gate is None:
                    tripped = self._observe(ref, command, breaker.UNCHANGED, result.message)
                    if tripped is not None:
                        return self._stop(ref, tripped, gate=BREAKER)
                return self._stop(ref, result.message, gate=gate, operation=result.operation)
            report.progress.append(Progress(ref, command, result.message))
            after = next_view(self.conn, ref)
            if (after.status, after.command) == before and step.continuation is not Continuation.CONFIRM_EACH:
                tripped = self._observe(ref, command, breaker.UNCHANGED, result.message)
                return self._stop(ref, tripped or f"{UNCHANGED}：{result.message}",
                                  gate=BREAKER if tripped else None)
            self._observe(ref, command, breaker.PROGRESS, result.message)

    def _observe(self, ref: SubjectRef, command: str, outcome: str, reason: str) -> str | None:
        return None if self.observe is None else self.observe(ref.id, command, outcome, reason)


# --from 重来

def restart(conn: sqlite3.Connection, layout: WorkspaceLayout, clock: Clock, config: ProjectConfig, ref: SubjectRef,
            stage: Stage, retriage: Callable[[str], None], zone: tzinfo | None = None) -> list[SubjectRef]:
    """从 stage 重来，返回之后续跑的对象。"""
    if stage not in FROM_STAGES:
        raise ValueError(f"--from 只接受 {'、'.join(item.value for item in FROM_STAGES)}")
    if stage is Stage.TRIAGE:
        problem_ids = [ref.id] if ref.kind == PROBLEM else list(issues.get(conn, ref.id).issue.problems)
        if not problem_ids:
            raise ValueError(f"Issue {ref.id} 没有关联问题(用户需求)，不能从 triage 重来；用 --from fix 重新修复")
        for problem_id in problem_ids:
            retriage(problem_id)
        return [SubjectRef(PROBLEM, problem_id) for problem_id in problem_ids]
    if ref.kind != ISSUE:
        raise ValueError(f"--from {stage.value} 只用于 Issue")
    env = IssueEnv(conn, layout, clock, config, zone)
    target = IssueStatus.TODO if stage is Stage.FIX else IssueStatus.IN_PROGRESS
    transitions.apply_event(env, transitions.record_of(env, ref.id), IssueEvent.RESTART,
                            IssueContext(restart_to=target), actor=ACTOR, note=f"从 {stage.value} 重来")
    now = clock.now()
    if stage is Stage.FIX:
        handoffs.mark_stale(conn, RunStage.FIX, ref.id, now)
    for phase in VerifyPhase:
        handoffs.mark_stale(conn, RunStage.VERIFY, ref.id, now, phase)
    handoffs.mark_stale(conn, RunStage.RELEASE, ref.id, now)
    return [ref]

