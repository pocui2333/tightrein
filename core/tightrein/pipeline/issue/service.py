"""IssueService(architecture/06 9、10、12)：create、sync、list、show、edit、approve、close、reopen、reindex、rerender。

create 为去向是提 Issue 且还没有 Issue 的问题逐个处理：先按代码快照补全分诊结论中的代码位置(pipeline/common/locations.py)；
按根因位置找到未关闭的同一根因 Issue 时追加，否则新建(状态为待决定，等用户放行；标题取取证输出 report.title，正文按
project.language 的模板)。新建前核对正文中引用的代码位置：补不全或「完整证据」一节的位置不存在时不建 Issue，交接文档
为 failed 并提示重新分诊；写好后做「提 Issue 与报告」一项的代码检查(结果写 scores)，不通过说明生成逻辑有缺陷，交接文档为
failed 且保留文件；P0 立即发本机通知(同一天同一 Issue 只通知一次)；写 issue-<问题编号>.json。
--output 模式照常读数据库，Issue 文件与交接文档写到输出目录，不写数据库、不发通知。
approve 放行 Issue 后的建分支确认由编排层组合(architecture/06 9.2)，本服务只做状态转换，不调用任何 vcs 写操作。
create_manual 直接新建用户需求的 Issue(architecture/06 10.7)：不关联问题，状态为待修，不写运行记录与交接文档，
不做「提 Issue 与报告」的检查(评分表针对分诊生成的正文)；--output 模式不支持。
关卡 gates.issue-approve 为 auto 时(architecture/06 9.3)，新建的 Issue 按分诊结论自动放行并调用 prepare(由组装层注入 fix prepare)，
不满足规则的留在待决定并写明需要用户决定的原因；交接文档记 autonomy。用户需求的 Issue 创建即视为已放行，同样
调用 prepare 申请建修复分支。
rerender 用现有数据按当前模板重写 Issue 的正文(不调用模型)，旧版式由此转为交接文档版式：分诊 Issue 取第一个问题的
分诊交接文档重写标题与正文，用户需求的 Issue 把原「需求」与「验收标准」转为「问题」与「验收标准」；都保留原「历史」并
追加一行。之后更新已有镜像的标题与正文。返回每个 Issue 缺少的新字段(要得到完整格式需重新分诊)。
issues.tracker 为 github 时(architecture/06 10.8)，create、create_manual、approve、close、reopen 之后对齐涉及的
Issue 的 GitHub 镜像，sync 之后对齐全部；镜像失败只记录，不影响本地结果。
"""

from __future__ import annotations

import sqlite3
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.contracts import versions
from tightrein.contracts.validate import check as check_schema
from tightrein.domain import ids, issue_sections
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import (
    CloseReason,
    HandoffStatus,
    IssueEvent,
    IssueStatus,
    RunStage,
    RunStatus,
    ScoreResult,
    Severity,
    TaskType,
    Stage,
    Verdict,
)
from tightrein.domain.problem import Problem
from tightrein.domain.run import Run
from tightrein.domain.signal import Signal
from tightrein.domain.triage import urgency_key
from tightrein.domain.handoff.document import Reference
from tightrein.domain.issue import Issue
from tightrein.domain.issue_sections import REFERENCES
from tightrein.evaluation.scorers.base import ItemResult
from tightrein.evaluation.scorers.code import section_location_problems
from tightrein.observability.events import EventLog
from tightrein.observability.notify import SENT, Notifier
from tightrein.observability.tracing import Tracer
from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.orchestrator.policy import autonomy, lanes
from tightrein.pipeline.common import locations
from tightrein.orchestrator.policy.autonomy import Decision
from tightrein.pipeline.issue.render import issue as template
from tightrein.pipeline.issue.render.summary import summary
from tightrein.pipeline.issue.steps import acceptance, append, body, create, edit, frontmatter, locate
from tightrein.pipeline.issue.steps import select, slug
from tightrein.pipeline.issue.steps import sync as sync_step
from tightrein.pipeline.issue.steps import transitions
from tightrein.pipeline.issue.steps.edit import EditResult
from tightrein.pipeline.issue.steps.github import GithubMirror, MirrorReport
from tightrein.pipeline.issue.steps.sync import SyncReport
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.store import sequences
from tightrein.store.files import atomic, handoff_files, issue_files, markdown
from tightrein.store.files.issue_files import IssueDocument, ReindexReport
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.markdown import MarkdownDocument
from tightrein.store.repos import issues, problems, runs, scores, signals
from tightrein.store.repos.issues import IssueRecord
from tightrein.store.repos.scores import ScoreRecord

STAGE = RunStage.ISSUE
ENVELOPE = "handoff/envelope.schema.json"
CREATED = "created"
APPENDED = "appended"
P0_EVENT = "issue-p0"
NEXT_ACTION = "等用户审阅：tightrein issue show {issue_id}，确认后 tightrein approve {issue_id}"
AUTO_APPROVED = "自动放行：满足"
APPROVED_NEXT = "已自动放行，建修复分支后进入修复：tightrein fix start {issue_id}"
NEEDS_DECISION = "需要用户决定"
RERENDERED = "按当前模板重新渲染(issue rerender，未重新分诊)"
MANUAL_APPROVED = "自动放行(gates.issue-approve 为 auto)：用户需求直接申请建修复分支"
LOCATED_KEYS = ("evidence", "rootCauses", "report", "worth", "flags")
BLOCKED_NEXT = "代码位置不完整，没有建 Issue：tightrein problem retriage {problem_id} 重新取证后再 tightrein issue create"


@dataclass
class IssueDeps:
    layout: WorkspaceLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    notifier: Notifier | None = None
    snapshot: Path | None = None
    zone: tzinfo | None = None
    mirror: GithubMirror | None = None
    prepare: Callable[[str], Any] | None = None


@dataclass(frozen=True)
class IssueItem:
    problem_id: str
    status: HandoffStatus
    issue_id: str | None = None
    action: str | None = None
    path: Path | None = None
    handoff: Path | None = None
    reason: str | None = None
    approved: bool = False


@dataclass
class IssueRun:
    run: Run | None
    items: list[IssueItem] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    summary: str = ""
    plan: list[tuple[str, str]] | None = None

    @property
    def exit_code(self) -> int:
        return 1 if any(item.status is HandoffStatus.FAILED for item in self.items) else 0


@dataclass(frozen=True)
class RerenderItem:
    issue_id: str
    rewritten: bool
    missing: list[str]


@dataclass
class Rerendered:
    items: list[RerenderItem] = field(default_factory=list)
    mirror: MirrorReport | None = None


@dataclass(frozen=True)
class IssueView:
    record: IssueRecord
    body: str
    problems: tuple[Problem, ...]


class IssueService:
    def __init__(self, deps: IssueDeps) -> None:
        self.deps = deps

    @property
    def output_mode(self) -> bool:
        return self.deps.layout.output_dir is not None

    @property
    def env(self) -> IssueEnv:
        deps = self.deps
        return IssueEnv(deps.conn, deps.layout, deps.clock, deps.config, deps.zone)

    # create

    def create(self, select_ids: Sequence[str] = (), input: Path | None = None, dry_run: bool = False) -> IssueRun:
        deps = self.deps
        chosen: list[tuple[Problem, dict[str, Any] | None]] = []
        skipped: list[tuple[str, str]] = []
        if input is not None:
            problem_id, outputs = select.from_input(input)
            found = problems.get(deps.conn, problem_id)
            if found is None:
                skipped.append((problem_id, f"{problem_id} 不存在"))
            else:
                chosen.append((found, outputs))
        elif select_ids:
            for problem_id in select_ids:
                found = problems.get(deps.conn, problem_id)
                reason = f"{problem_id} 不存在" if found is None else select.eligible(deps.conn, found)
                if reason is not None or found is None:
                    skipped.append((problem_id, reason or f"{problem_id} 不存在"))
                else:
                    chosen.append((found, None))
        else:
            chosen = [(problem, None) for problem in select.pending(deps.conn)]
        if dry_run:
            plan = []
            for problem, outputs in chosen:
                outputs = outputs or select.triage_outputs(deps.layout, deps.conn, problem.id)
                existing = locate.find(deps.conn, outputs.get("rootCauses") or [])
                plan.append((problem.id, f"追加到 {existing.issue.id}" if existing else "新建"))
            return IssueRun(None, skipped=skipped, plan=plan)
        started = deps.clock.now()
        run_id = runs.free_id(deps.conn, started, STAGE)
        tracer = Tracer(deps.events, deps.clock, run_id=run_id, stage=STAGE.value)
        run = Run(run_id, STAGE, started, RunStatus.RUNNING, trace_id=tracer.trace_id)
        if not self.output_mode:
            runs.save(deps.conn, run)
        items = []
        for problem, outputs in chosen:
            with tracer.span("run_script", attributes={"step": "create", "problemId": problem.id}):
                items.append(self._create_one(run, problem, outputs, tracer))
        failed = [f"{item.problem_id}：{item.reason}" for item in items if item.status is HandoffStatus.FAILED]
        text = summary(run.id, [item.issue_id for item in items if item.action == CREATED and item.issue_id],
                       [item.issue_id for item in items if item.action == APPENDED and item.issue_id], failed,
                       len([record for record in issues.find(deps.conn, status=IssueStatus.NEEDS_DECISION)
                            if record.issue.hold is None]))
        run = replace(run, status=RunStatus.PARTIAL if failed else RunStatus.OK, ended_at=deps.clock.now())
        if not self.output_mode:
            runs.save(deps.conn, run)
            atomic.write_text(deps.layout.run_report(run.id), text)
            for item in items:
                if item.approved and deps.prepare is not None and item.issue_id is not None:
                    prepared = deps.prepare(item.issue_id)
                    if prepared.status is HandoffStatus.FAILED:
                        transitions.annotate(self.env, item.issue_id, f"建修复分支失败：{prepared.message}")
            self.mirror([item.issue_id for item in items if item.issue_id and item.status is not HandoffStatus.FAILED])
        return IssueRun(run, items, skipped, text)

    def _create_one(self, run: Run, problem: Problem, outputs: dict[str, Any] | None, tracer: Tracer) -> IssueItem:
        deps = self.deps
        try:
            outputs, unresolved = self._located(outputs or select.triage_outputs(deps.layout, deps.conn, problem.id))
            latest = self._latest(problem)
            criteria = acceptance.build(problem, latest, deps.config.language)
            existing = locate.find(deps.conn, outputs.get("rootCauses") or [])
            if existing is None:
                blocked = [*unresolved, *self._location_problems(run, problem, outputs, criteria)]
                if blocked:
                    return self._blocked(run, problem, blocked)
            if self.output_mode:
                record, action = self._write_output(run, problem, outputs, latest, criteria, existing)
                root = deps.layout.output_dir or deps.layout.root
            elif existing is not None:
                record = append.append(deps.conn, deps.layout, deps.clock, existing, problem, outputs, run.id,
                                       deps.zone)
                action, root = APPENDED, deps.layout.root
            else:
                name = slug.slug(problem, latest, deps.config.whole_threshold("issue.slugMaxLength"))
                record = create.create(
                    deps.conn, deps.layout, deps.clock, run_id=run.id, problem=problem, outputs=outputs, slug=name,
                    title=self._title(outputs),
                    render=lambda issue_id: self._document(problem, outputs, criteria, issue_id,
                                                           self._first_history(run, outputs)),
                    source=self._source(problem, latest))
                action, root = CREATED, deps.layout.root
            items = frontmatter.check(root, record.path, deps.snapshot, deps.clock)
            if not self.output_mode:
                for item in items:
                    scores.append(deps.conn, ScoreRecord(Stage.ISSUE, run.id, problem.id, 1, item.item_id,
                                                         item.result, item.method, deps.clock.now(), item.reason))
            notified = self._notify(record) if not self.output_mode else False
            passed = all(item.result is not ScoreResult.FAIL for item in items)
            decided = self._decide(record.issue.id, tracer) if action == CREATED and passed and not self.output_mode \
                else None
            return self._handoff(run, problem, record, action, criteria, outputs, notified, items, root, decided)
        except Exception as error:  # 单个问题的程序异常只影响这个问题
            detail = "".join(traceback.format_exception_only(type(error), error)).strip()
            document = self._envelope(run, problem.id, {}, HandoffStatus.FAILED,
                                      "查看失败原因，修正后重新执行 tightrein issue create", detail)
            written = handoff_files.write(deps.layout, document, deps.clock,
                                          conn=None if self.output_mode else deps.conn)
            return IssueItem(problem.id, HandoffStatus.FAILED, handoff=written.path, reason=detail)

    def _located(self, outputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """按代码快照补全分诊结论中的代码位置；没有快照时原样返回。"""
        if self.deps.snapshot is None:
            return outputs, []
        completed = locations.complete(outputs, self.deps.snapshot, LOCATED_KEYS)
        return dict(completed.value), completed.problems()

    def _title(self, outputs: dict[str, Any]) -> str:
        return body.title(outputs, self.deps.config.whole_threshold("issue.titleMaxLength"))

    def _source(self, problem: Problem, latest: Signal | None) -> str:
        """来源：探针与最近信号所属采集运行的档位。"""
        found = runs.get(self.deps.conn, latest.run_id) if latest is not None else None
        level = found.level.value if found is not None and found.level is not None else None
        return f"{problem.probe.value}" + (f"({level})" if level else "")

    def _document(self, problem: Problem, outputs: dict[str, Any], criteria: list[str], issue_id: str, history: str,
                  extra: Sequence[Problem] = ()) -> str:
        """交接文档版式的正文；extra 为追加进来的其他关联问题(rerender 时)。"""
        deps = self.deps
        language = deps.config.language
        task_type = TaskType(outputs["taskType"]) if outputs.get("taskType") else None
        content = body.sections(outputs, criteria, language, lanes.writes_repro_test(deps.config, task_type))
        findings = deps.layout.relative(deps.layout.finding(problem.id))
        references = [*body.references(outputs, findings, problem, language),
                      *(Reference(other.id, other.title) for other in extra)]
        return template.document(body.conclusion(outputs, self._title(outputs), language), content, references,
                                 [template.approve_step(issue_id, language)], history, language)

    def _location_problems(self, run: Run, problem: Problem, outputs: dict[str, Any],
                           criteria: list[str]) -> list[str]:
        """新建前核对正文「引用」中完整证据的代码位置；没有快照时不核对。"""
        if self.deps.snapshot is None:
            return []
        text = self._document(problem, outputs, criteria, "—", self._first_history(run, outputs))
        section = issue_sections.find(issue_sections.split(text), REFERENCES) or ""
        return section_location_problems(self.deps.snapshot, section)[1]

    def _blocked(self, run: Run, problem: Problem, reasons: list[str]) -> IssueItem:
        deps = self.deps
        reason = "；".join(dict.fromkeys(reasons))
        document = self._envelope(run, problem.id, {}, HandoffStatus.FAILED,
                                  BLOCKED_NEXT.format(problem_id=problem.id), reason)
        written = handoff_files.write(deps.layout, document, deps.clock, conn=None if self.output_mode else deps.conn)
        return IssueItem(problem.id, HandoffStatus.FAILED, handoff=written.path, reason=reason)

    def _latest(self, problem: Problem) -> Signal | None:
        found = signals.get_many(self.deps.conn, problems.signal_ids(self.deps.conn, problem.id))
        return max(found, key=lambda signal: (signal.occurred_at, signal.id)) if found else None

    def _first_history(self, run: Run, outputs: dict[str, Any]) -> str:
        verdict = Verdict(outputs["verdict"]).label if outputs.get("verdict") else "无"
        return template.history_line(self.deps.clock.now(), f"创建(运行 {run.id})：分诊判定为{verdict}，严重度 "
                                     f"{outputs.get('severity') or '无'}，取证 commit {outputs['triageCommit']}",
                                     self.deps.zone)

    def _write_output(self, run: Run, problem: Problem, outputs: dict[str, Any], latest: Signal | None,
                      criteria: list[str], existing: IssueRecord | None) -> tuple[IssueRecord, str]:
        """--output 模式：新建或追加后的 Issue 文件写到输出目录，不写数据库。"""
        deps = self.deps
        if existing is not None:
            current = issue_files.read(deps.layout.root / existing.path)
            document = append.appended(current, problem, outputs, run.id, deps.clock, deps.zone)
            action = APPENDED
        else:
            issue_id = ids.issue_id(sequences.current(deps.conn, sequences.ISSUE) + 1)
            name = slug.slug(problem, latest, deps.config.whole_threshold("issue.slugMaxLength"))
            findings = deps.layout.relative(deps.layout.finding(problem.id))
            issue = frontmatter.issue_for(issue_id, name, self._title(outputs), problem, outputs, deps.clock.now(),
                                          findings, self._source(problem, latest))
            text = self._document(problem, outputs, criteria, issue_id, self._first_history(run, outputs))
            document, action = IssueDocument(issue, text, run.id), CREATED
        data = issue_files.to_frontmatter(document.issue, document.run_id)
        check_schema(issue_files.SCHEMA, data)
        path = deps.layout.output_issue_file(document.issue.id, document.issue.slug)
        text = markdown.write(path, MarkdownDocument(data, document.body))
        relative = path.relative_to(deps.layout.output_dir or deps.layout.root).as_posix()
        return IssueRecord(document.issue, relative, issue_files.content_hash(text)), action

    def _notify(self, record: IssueRecord) -> bool:
        if record.issue.severity is not Severity.P0 or self.deps.notifier is None:
            return False
        text = f"P0 Issue {record.issue.id}：{record.issue.title}"
        result = self.deps.notifier.notify(P0_EVENT, record.issue.id, text)
        return result.status == SENT

    def _envelope(self, run: Run, problem_id: str, outputs: dict[str, Any], status: HandoffStatus, next_action: str,
                  reason: str | None = None) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schemaVersion": versions.current(ENVELOPE), "runId": run.id, "stage": STAGE.value,
            "subject": {"type": "problem", "id": problem_id}, "status": status.value, "inputsRef": {},
            "outputs": outputs, "nextAction": next_action, "createdAt": format_iso(self.deps.clock.now()),
        }
        if reason is not None:
            document["blockedReason"] = reason
        return document

    def _decide(self, issue_id: str, tracer: Tracer) -> Decision | None:
        """关卡 gates.issue-approve 为 auto 时按分诊结论自动放行(放行后由 create 调用 prepare)，否则留在待决定并写明需要用户决定的原因；
        理由写进 Issue 历史(放行事件的备注)、GitHub 镜像评论与 gate 事件。"""
        deps = self.deps
        if not gates.auto(deps.config, Gate.ISSUE_APPROVE):
            return None
        record = transitions.record_of(self.env, issue_id)
        triaged = []
        for problem_id in record.issue.problems:
            try:
                triaged.append(select.triage_outputs(deps.layout, deps.conn, problem_id))
            except LookupError:
                continue
        decision = autonomy.approval(deps.config, triaged)
        text = decision.text(AUTO_APPROVED, NEEDS_DECISION)
        tracer.event("gate", decision=autonomy.AUTO_APPROVE if decision.approved else autonomy.NEEDS_DECISION,
                     reason=text, artifact=issue_id)
        if decision.approved:
            transitions.apply_event(self.env, record, IssueEvent.APPROVE, actor=autonomy.ACTOR, note=text)
        else:
            transitions.annotate(self.env, issue_id, text)
        return decision

    def _handoff(self, run: Run, problem: Problem, record: IssueRecord, action: str, criteria: list[str],
                 outputs: dict[str, Any], notified: bool, items: list[ItemResult], root: Path,
                 decided: Decision | None = None) -> IssueItem:
        deps = self.deps
        failed = [f"[{item.item_id}] {item.reason}" for item in items if item.result is ScoreResult.FAIL]
        issue = record.issue
        extra = {"autonomy": decided.to_dict()} if decided is not None else {}
        document = self._envelope(
            run, problem.id,
            {"issueId": issue.id, "action": action, "path": record.path, "problems": list(issue.problems),
             "severity": issue.severity.value, "treatment": outputs.get("treatment"),
             "labels": list(outputs.get("labels") or []), "acceptance": criteria, "notified": notified, **extra},
            HandoffStatus.FAILED if failed else HandoffStatus.OK,
            "Issue 文件没有通过检查，查看后修正生成逻辑" if failed else (
                APPROVED_NEXT if decided is not None and decided.approved else NEXT_ACTION).format(issue_id=issue.id),
            "；".join(failed) if failed else None)
        written = handoff_files.write(deps.layout, document, deps.clock, conn=None if self.output_mode else deps.conn)
        return IssueItem(problem.id, HandoffStatus(document["status"]), issue.id, action, root / record.path,
                         written.path, "；".join(failed) or None, decided is not None and decided.approved)

    def create_manual(self, title: str, requirement: str, severity: Severity,
                      task_type: TaskType | None = None) -> IssueRecord:
        deps = self.deps
        if self.output_mode:
            raise transitions.IssueCommandRejected("用户需求的 Issue 不支持 --output 模式")
        if not title.strip() or not requirement.strip():
            raise transitions.IssueCommandRejected("用户需求的 Issue 需要非空的标题与正文")
        record = create.create_manual(deps.conn, deps.layout, deps.clock, title=title.strip(), requirement=requirement,
                                      severity=severity,
                                      slug_max_length=deps.config.whole_threshold("issue.slugMaxLength"),
                                      language=deps.config.language, zone=deps.zone, task_type=task_type,
                                      repro_test=lanes.writes_repro_test(deps.config, task_type))
        self._notify(record)
        if gates.auto(deps.config, Gate.ISSUE_APPROVE):
            transitions.annotate(self.env, record.issue.id, MANUAL_APPROVED)
        self.mirror([record.issue.id])
        if gates.auto(deps.config, Gate.ISSUE_APPROVE) and deps.prepare is not None:
            deps.prepare(record.issue.id)
        return transitions.record_of(self.env, record.issue.id)

    def mirror(self, issue_ids: Sequence[str] | None = None) -> MirrorReport | None:
        """对齐 GitHub 镜像；没有配置镜像或 issue_ids 为空列表时什么都不做。"""
        if self.deps.mirror is None or self.output_mode or issue_ids == []:
            return None
        return self.deps.mirror.sync(None if issue_ids is None else list(issue_ids))

    # 其余命令

    def sync(self) -> SyncReport:
        report = sync_step.sync(self.env)
        report.github = self.mirror()
        return report

    def list(self, status: IssueStatus | None = None, severity: Severity | None = None) -> list[IssueRecord]:
        found = [record for record in issues.find(self.deps.conn, status=status)
                 if severity is None or record.issue.severity is severity]
        return sorted(found, key=lambda record: (*urgency_key(record.issue.treatment, record.issue.severity),
                                                 record.issue.created_at))

    def show(self, issue_id: str) -> IssueView:
        record = transitions.record_of(self.env, issue_id)
        document = issue_files.read(self.deps.layout.root / record.path)
        found = tuple(problem for problem in (problems.get(self.deps.conn, pid) for pid in record.issue.problems)
                      if problem is not None)
        return IssueView(record, document.body, found)

    def edit(self, issue_id: str, editor: Callable[[Path], None], retry: Callable[[list[str]], bool]) -> EditResult:
        return edit.edit(self.env, issue_id, editor, retry)

    def approve(self, issue_id: str, note: str | None = None) -> IssueRecord:
        record = transitions.approve(self.env, issue_id, note)
        self.mirror([issue_id])
        return record

    def close(self, issue_id: str, reason: CloseReason, *, note: str | None = None,
              duplicate_of: str | None = None) -> IssueRecord:
        record = transitions.close(self.env, issue_id, reason, note=note, duplicate_of=duplicate_of)
        self.mirror([issue_id, *([duplicate_of] if duplicate_of else [])])
        return record

    def reopen(self, issue_id: str, note: str | None = None) -> IssueRecord:
        record = transitions.reopen(self.env, issue_id, note)
        self.mirror([issue_id])
        return record

    def reindex(self) -> ReindexReport:
        return issue_files.reindex(self.deps.conn, self.deps.layout)

    def rerender(self, issue_ids: Sequence[str] = ()) -> Rerendered:
        """issue_ids 为空时处理全部未关闭的 Issue。"""
        deps = self.deps
        if self.output_mode:
            raise transitions.IssueCommandRejected("issue rerender 不支持 --output 模式")
        chosen = [transitions.record_of(self.env, issue_id) for issue_id in issue_ids] if issue_ids else [
            record for record in issues.find(deps.conn) if not record.issue.is_closed]
        result = Rerendered()
        for record in chosen:
            if record.issue.is_manual:
                result.items.append(self._rerender_manual(record))
                continue
            result.items.append(self._rerender_one(record))
        result.mirror = self.deps.mirror.refresh([record.issue.id for record in chosen]) \
            if deps.mirror is not None and chosen else None
        return result

    def _rewrite(self, current: IssueDocument, issue: Issue, text: str, note: str) -> None:
        deps = self.deps
        text = template.append_history(text, deps.clock.now(), note, deps.zone)
        issue_files.write(deps.conn, deps.layout, IssueDocument(replace(issue, updated_at=deps.clock.now()), text,
                                                                current.run_id))

    def _rerender_one(self, record: IssueRecord) -> RerenderItem:
        deps = self.deps
        problem = problems.get(deps.conn, record.issue.problems[0])
        if problem is None:
            raise LookupError(f"Issue {record.issue.id} 的问题 {record.issue.problems[0]} 不存在")
        outputs, _ = self._located(select.triage_outputs(deps.layout, deps.conn, problem.id))
        latest = self._latest(problem)
        criteria = acceptance.build(problem, latest, deps.config.language)
        current = issue_files.read(deps.layout.root / record.path)
        history = issue_sections.find(issue_sections.split(current.body), issue_sections.HISTORY) or ""
        extra = [found for pid in record.issue.problems[1:] if (found := problems.get(deps.conn, pid)) is not None]
        text = self._document(problem, outputs, criteria, record.issue.id, history, extra)
        missing = body.missing_fields(outputs)
        note = RERENDERED + (f"；缺少 {'、'.join(missing)}，重新分诊后可补全" if missing else "")
        issue = replace(current.issue, title=self._title(outputs),
                        source=current.issue.source or self._source(problem, latest))
        self._rewrite(current, issue, text, note)
        return RerenderItem(record.issue.id, True, missing)

    def _rerender_manual(self, record: IssueRecord) -> RerenderItem:
        """用户需求的 Issue：旧版式的「需求」与「验收标准」转为交接文档版式；已是新版式的不改正文。"""
        deps = self.deps
        current = issue_files.read(deps.layout.root / record.path)
        if issue_sections.is_handoff(current.body):
            return RerenderItem(record.issue.id, False, [])
        found = issue_sections.split(current.body)
        requirement = issue_sections.find(found, issue_sections.REQUIREMENT) or ""
        criteria = issue_sections.find(found, issue_sections.ACCEPTANCE)
        if criteria:
            requirement += f"\n\n## {issue_sections.heading(issue_sections.ACCEPTANCE, deps.config.language)}\n\n" \
                           f"{criteria}"
        history = issue_sections.find(found, issue_sections.HISTORY) or ""
        issue = current.issue
        text = template.manual_document(issue.id, issue.title, requirement, history, deps.config.language,
                                        repro_test=lanes.writes_repro_test(deps.config, issue.task_type))
        self._rewrite(current, replace(issue, source=issue.source or create.MANUAL_SOURCE), text, RERENDERED)
        return RerenderItem(record.issue.id, True, [])
