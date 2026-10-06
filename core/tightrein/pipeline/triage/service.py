"""TriageService(architecture/06 3、4、7，redesign/03-triage.md)：选择问题、逐个处理、定去向与落库、写交接文档与发现报告。

- 一次运行先把只读 worktree 切换到取证 commit(缺省为 origin/<主分支> 的最新 commit)，切换失败时整次停止；
- 每个问题：组装主张 → main 差异 → 查重(并入其他问题时到此为止) → 取证与证据检查(一次给出判定、严重度、价值判断、
  任务类型、预估规模与修复方向) → 高风险时证伪复核(triage.refute) → 归因 → 评级与决策树给出处理标签 → 定去向、落库
  (一个事务)并写交接文档与发现报告；
- 当天费用达到 stages.triage.budgetPerDay 时不再开始新的问题；问题的对象锁被占用时跳过；单个问题出现程序异常时
  交接文档为 failed，其他问题继续；
- --output 模式照常读数据库，交接文档与发现报告写到输出目录，不写数据库、不加锁、不写 suppressions.yaml；
  --dry-run 只列出选中的问题与将调用的角色。
retriage 对已分诊的问题重新分诊(可带用户补充的信息)；override 是用户改判，不调用角色。
"""

from __future__ import annotations

import sqlite3
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.contracts import versions
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import (
    ContextKind,
    Disposition,
    HandoffStatus,
    ProblemEvent,
    RunStage,
    RunStatus,
    Severity,
    Stage,
    Treatment,
    TriageOutcome,
    Verdict,
    WorthRecommendation,
)
from tightrein.domain.problem import Problem
from tightrein.domain.run import Run
from tightrein.domain.triage import TriageResult
from tightrein.observability.events import EventLog
from tightrein.observability.tracing import Tracer
from tightrein.runner.roles import Overrides
from tightrein.pipeline.triage.prompts.claim_verifier import CLAIM_VERIFIER, REFUTER
from tightrein.pipeline.triage.prompts.common import PromptContext, RoleCalls
from tightrein.pipeline.triage.render import findings
from tightrein.pipeline.triage.render.summary import SummaryItem, summary
from tightrein.pipeline.triage.steps import (
    attribution,
    claims,
    dedup,
    disposition,
    evidence,
    main_diff,
    persist,
    rating,
    refute,
    select,
)
from tightrein.pipeline.triage.steps.case import CONFIRMING, TriageCase
from tightrein.pipeline.triage.steps.disposition import Decision
from tightrein.pipeline.triage.steps.persist import handoff_outputs
from tightrein.pipeline.triage.steps.tradeoff import tradeoff_valid
from tightrein.retrieval.context import EMPTY, ContextBundle, ContextRequest
from tightrein.runner import limits
from tightrein.runner.service import Runner
from tightrein.store import locks
from tightrein.store.files import atomic, handoff_files
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.repos import handoffs, problems, runs, signals, triage
from tightrein.store.repos.problem_events import OPERATION_USER
from tightrein.vcs.errors import VcsError

STAGE = RunStage.TRIAGE
STEP = "run_script"
ENVELOPE = "handoff/envelope.schema.json"
DEDUP_ROLE = "triage-dedup"
LOCKED = "正被其他运行处理"
BUDGET = "当天 triage 的费用已达到 stages.triage.budgetPerDay，留到下一次"
SYNC_HINT = "先执行 tightrein project worktree sync"
NEXT_ACTIONS = {
    Disposition.CREATE_ISSUE: "交给 issue 创建",
    Disposition.AWAITING_DEPLOY: "等待部署后由 aggregate 判定已解决",
    Disposition.FALSE_POSITIVE: "已判为误报并生成抑制规则",
    Disposition.ACCEPTED_TRADEOFF: "命中已接受的取舍，已忽略",
    Disposition.DEFERRED: "暂不修，再出现或严重度升级时重新分诊",
    Disposition.MANUAL_QUEUE: "进入人工队列：补充信息后执行 tightrein problem retriage <问题> --note，或直接改判",
}
ContextFor = Callable[[ContextRequest], ContextBundle]


class OverrideRejected(ValueError):
    """改判的前提不满足：问题不存在、已被合并，或没有可以沿用的取证 commit。"""


@dataclass(frozen=True)
class TriageRequest:
    select: tuple[str, ...] = ()
    limit: int | None = None
    commit: str | None = None
    input: Path | None = None
    ignore_state: bool = False
    dry_run: bool = False
    overrides: Overrides = Overrides()


@dataclass
class TriageDeps:
    layout: WorkspaceLayout
    tool: ToolLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    runner: Runner
    git: Any
    sync: Callable[[str | None], str]
    prs: attribution.PullReader | None = None
    context: ContextFor | None = None
    zone: tzinfo | None = None


@dataclass(frozen=True)
class TriageItem:
    problem_id: str
    title: str
    status: HandoffStatus
    verdict: Verdict | None = None
    severity: Severity | None = None
    disposition: Disposition | None = None
    handoff: Path | None = None
    findings: Path | None = None
    merged_into: str | None = None
    reason: str | None = None

    def summary_item(self) -> SummaryItem:
        return SummaryItem(self.problem_id, self.title, self.status, self.verdict, self.severity, self.disposition,
                           self.merged_into, self.reason)


@dataclass(frozen=True)
class PlannedProblem:
    problem_id: str
    title: str
    role: str


@dataclass(frozen=True)
class QueueItem:
    problem_id: str
    title: str
    verdict: Verdict
    missing_info: tuple[str, ...]
    findings: str


@dataclass
class TriageRun:
    run: Run | None
    items: list[TriageItem] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    summary: str = ""
    message: str | None = None
    plan: list[PlannedProblem] | None = None
    blocked: str | None = None  # 有待分诊的问题却无法开始(只读 worktree 无法切换)的原因；编排记为这一步失败

    @property
    def exit_code(self) -> int:
        failed = self.run is not None and self.run.status is RunStatus.FAILED
        return 1 if failed or any(item.status is HandoffStatus.FAILED for item in self.items) else 0


@dataclass
class _Work:
    run: Run
    tracer: Tracer
    calls: RoleCalls
    commit: str
    notes: list[str] = field(default_factory=list)


class TriageService:
    def __init__(self, deps: TriageDeps) -> None:
        self.deps = deps

    @property
    def output_mode(self) -> bool:
        return self.deps.layout.output_dir is not None

    # 入口

    def run(self, request: TriageRequest) -> TriageRun:
        chosen = list(request.select)
        if request.input is not None:
            chosen.append(handoff_files.read(request.input)["subject"]["id"])
        return self._run(tuple(chosen), request, retriage=False, notes={})

    def retriage(self, problem_id: str, note: str | None = None, overrides: Overrides = Overrides()) -> TriageRun:
        previous = self._previous_outputs(problem_id)
        notes = list(previous["claim"]["userNotes"]) if previous else []
        if note:
            notes.append(note)
        return self._run((problem_id,), TriageRequest(select=(problem_id,), overrides=overrides), retriage=True,
                         notes={problem_id: notes})

    def queue(self) -> list[QueueItem]:
        found = []
        for problem in problems.find(self.deps.conn, statuses=select.TRIAGEABLE):
            record = triage.latest(self.deps.conn, problem.id)
            if record is None or record.result.disposition is not Disposition.MANUAL_QUEUE:
                continue
            outputs = self._previous_outputs(problem.id) or {}
            missing = tuple(item["item"] for item in outputs.get("missingInfo", []))
            found.append(QueueItem(problem.id, problem.title, record.result.verdict, missing,
                                   self.deps.layout.relative(self.deps.layout.finding(problem.id))))
        return found

    def override(self, problem_id: str, verdict: Verdict, reason: str, *, severity: Severity | None = None,
                 chosen: Disposition | None = None) -> TriageRun:
        deps = self.deps
        problem = problems.get(deps.conn, problem_id)
        if problem is None or problem.merged_into is not None:
            raise OverrideRejected(f"{problem_id} 不存在" if problem is None else
                                   f"{problem_id} 已并入 {problem.merged_into}，改判请针对 {problem.merged_into}")
        previous = triage.latest(deps.conn, problem_id)
        commit = previous.result.triage_commit if previous is not None else problem.last_seen_release
        if commit is None:
            raise OverrideRejected(f"{problem_id} 没有分诊记录，也不知道发现时的版本，先运行 tightrein triage")
        run, tracer = self._begin(commit)
        decision = disposition.override(deps.config, problem, verdict, chosen)
        level = severity or (previous.result.severity if previous is not None else None)
        kept = previous.result if previous is not None and verdict in CONFIRMING else None
        result = triage.TriageRecord(TriageResult(
            problem_id, triage.next_attempt(deps.conn, problem_id), verdict, decision.disposition, reason, commit,
            severity=level, outcome=TriageOutcome.OVERRIDDEN,
            root_causes=previous.result.root_causes if previous else (),
            introduced_by=previous.result.introduced_by if previous else None,
            treatment=kept.treatment if kept else None, task_type=kept.task_type if kept else None,
            size_tier=kept.size_tier if kept else None), run.id, deps.clock.now())
        if previous is not None and previous.result.verdict is Verdict.REFUTED and verdict in CONFIRMING:
            triage.set_outcome(deps.conn, problem_id, previous.result.attempt, TriageOutcome.FALSE_REFUTE,
                               deps.clock.now())
        persist.commit(deps.conn, deps.layout, deps.clock, deps.config, run_id=run.id, problem_id=problem_id,
                       event=ProblemEvent.OVERRIDDEN, context=decision.context, reason=reason, record=result,
                       operation=OPERATION_USER)
        tracer.event("user_action", decision=verdict.value, reason=reason, attributes={"problemId": problem_id})
        outputs = self._overridden_outputs(problem, result, reason)
        item = self._write(run, problem, outputs, decision, attempt=result.result.attempt)
        return self._end(run, [item], [], [])

    # 一次运行

    def _run(self, problem_ids: tuple[str, ...], request: TriageRequest, *, retriage: bool,
             notes: dict[str, list[str]]) -> TriageRun:
        deps = self.deps
        limit = request.limit or deps.config.whole_threshold("triage.perRun")
        selection = select.choose(deps.conn, limit, problem_ids, retriage=retriage, ignore_state=request.ignore_state)
        if not selection.chosen:
            message = "没有待分诊的问题" if not selection.rejected else "；".join(r for _, r in selection.rejected)
            return TriageRun(None, skipped=list(selection.rejected), message=message)
        if request.dry_run:
            plan = []
            for problem in selection.chosen:
                plan.append(PlannedProblem(problem.id, problem.title, CLAIM_VERIFIER))
            return TriageRun(None, skipped=list(selection.rejected), plan=plan)
        try:
            commit = deps.sync(request.commit)
        except VcsError as error:
            reason = f"只读 worktree 无法切换到取证 commit：{error}；{SYNC_HINT}"
            return TriageRun(None, skipped=list(selection.rejected), message=reason, blocked=reason)
        run, tracer = self._begin(commit)
        context = PromptContext(deps.tool, deps.config, run.id, deps.layout.readonly_worktree())
        work = _Work(run, tracer, RoleCalls(deps.runner, deps.clock, context,
                                            deps.config.whole_threshold("triage.evidenceRetries"),
                                            request.overrides, None if self.output_mode else deps.conn), commit)
        skipped = list(selection.rejected)
        items: list[TriageItem] = []
        held: list[str] = []
        try:
            for index, problem in enumerate(selection.chosen):
                if self._budget_reached():
                    skipped += [(item.id, BUDGET) for item in selection.chosen[index:]]
                    break
                if not self._lock(problem.id, run.id):
                    skipped.append((problem.id, LOCKED))
                    continue
                held.append(problem.id)
                try:
                    with tracer.span(STEP, attributes={"step": "examine", "problemId": problem.id}):
                        case = self._examine(work, problem, notes.get(problem.id, []))
                    items.append(self._finish(work, case))
                except Exception as error:  # 单个问题的程序异常只影响这个问题
                    items.append(self._failed(run, problem, error))
        finally:
            for problem_id in held:
                locks.release(deps.conn, problem_id)
        return self._end(run, items, skipped, work.notes)

    def _begin(self, commit: str) -> tuple[Run, Tracer]:
        deps = self.deps
        started = deps.clock.now()
        run_id = runs.free_id(deps.conn, started, STAGE)
        tracer = Tracer(deps.events, deps.clock, run_id=run_id, stage=STAGE.value)
        run = Run(run_id, STAGE, started, RunStatus.RUNNING, target_commit=commit, trace_id=tracer.trace_id)
        if not self.output_mode:
            runs.save(deps.conn, run)
        return run, tracer

    def _end(self, run: Run, items: list[TriageItem], skipped: list[tuple[str, str]], notes: list[str]) -> TriageRun:
        deps = self.deps
        failed = any(item.status is HandoffStatus.FAILED for item in items)
        run = replace(run, status=RunStatus.PARTIAL if failed else RunStatus.OK, ended_at=deps.clock.now())
        text = summary(run.id, [item.summary_item() for item in items], skipped, notes)
        if not self.output_mode:
            runs.save(deps.conn, run)
            atomic.write_text(deps.layout.run_report(run.id), text)
        return TriageRun(run, items, skipped, text)

    def _budget_reached(self) -> bool:
        deps = self.deps
        budget = limits.DailyBudget(deps.conn, deps.clock, deps.zone)
        return budget.exhausted(Stage.TRIAGE, limits.budget_per_day(deps.config, Stage.TRIAGE))

    def _lock(self, problem_id: str, run_id: str) -> bool:
        if self.output_mode:
            return True
        ttl = timedelta(minutes=self.deps.config.whole_threshold("triage.lockMinutes"))
        try:
            locks.acquire(self.deps.conn, problem_id, self.deps.clock, ttl, run_id=run_id)
        except locks.LockHeld:
            return False
        return True

    # 单个问题

    def _examine(self, work: _Work, problem: Problem, user_notes: Sequence[str]) -> TriageCase:
        deps = self.deps
        conn, worktree = deps.conn, deps.layout.readonly_worktree()
        found = signals.get_many(conn, problems.signal_ids(conn, problem.id))
        latest = select.latest_signal(conn, problem.id)
        p0 = select.estimated_p0(problem, latest)
        claim = claims.build(problem, found, user_notes, deps.config.whole_threshold("triage.claimSamples"))
        files = main_diff.candidate_files(claim, found)
        diff = main_diff.collect(deps.git, worktree, problem.last_seen_release, work.commit, files)
        claim = claim.with_fact(main_diff.LABEL, diff.value())
        knowledge = EMPTY
        if deps.context is not None:
            request = ContextRequest(ContextKind.TRIAGE, paths=files, problem_id=problem.id, keywords=claim.title)
            knowledge = deps.context(request).render()
        estimated = rating.complexity(deps.config, rating.hint(problem, latest, p0))
        case = TriageCase(problem, latest, found, p0, claim, estimated, knowledge,
                          triage.next_attempt(conn, problem.id),
                          select.retriage_requests(conn, problem.id))
        if problem.issue_id is None:
            candidates = dedup.candidates(conn, problem, files, deps.clock.now(),
                                          deps.config.whole_threshold("triage.dedupCandidateDays"),
                                          deps.config.whole_threshold("triage.dedupCandidates"))
            outcome = dedup.decide(work.calls, conn, problem, claim, candidates)
            case.record(DEDUP_ROLE, outcome.statuses)
            if outcome.note:
                case.notes.append(outcome.note)
            if outcome.target is not None and outcome.target != problem.id:
                case.merged_into = outcome.target
                return case
        self._judge(work, case)
        return case

    def _judge(self, work: _Work, case: TriageCase) -> None:
        deps = self.deps
        problem, latest = case.problem, case.latest
        # 用户请求的重新分诊(case.handled 非空)不复用采集时的取证，重新调用 claim-verifier
        prepared = evidence.prepared_output(latest) if evidence.reuses_prepared(problem) and not case.handled else None
        case.evidence = evidence.gather(work.calls, problem.id, case.claim, role=CLAIM_VERIFIER,
                                        complexity=case.complexity, knowledge=case.knowledge, prepared=prepared)
        case.record(case.evidence.role, case.evidence.statuses)
        case.needs_manual = False
        case.rating = rating.rate(deps.config, problem, latest, case.outputs, p0=case.p0)
        if not case.evidence.passed:
            case.verdict, case.needs_manual = Verdict.INSUFFICIENT, True
            case.notes.append(f"证据检查重做后仍未通过：{case.evidence.reason}")
            return
        verdict = Verdict(case.outputs["verdict"])
        if refute.needed(refute.RefuteRule.from_config(deps.config), verdict, p0=case.p0,
                         severity=case.rating.severity, task_type=case.rating.task_type,
                         impact_kind=rating.impact_kind(case.outputs)):
            case.refuter = evidence.gather(work.calls, problem.id, case.claim, role=REFUTER,
                                           complexity=case.complexity, knowledge=case.knowledge)
            case.record(REFUTER, case.refuter.statuses)
            second = Verdict(case.refuter.outputs["verdict"]) if case.refuter.passed else None
            case.refuter_verdict = second
            final, case.needs_manual = refute.combine(verdict, second)
            if final is not verdict and second in CONFIRMING:
                case.evidence = case.refuter
                case.rating = rating.rate(deps.config, problem, latest, case.outputs, p0=case.p0)
            if case.needs_manual:
                case.notes.append(f"证伪复核判为{second.label if second else '未通过证据检查'}，"
                                  f"与取证的{verdict.label}不一致")
            verdict = final
        case.verdict = verdict
        if verdict in CONFIRMING and not case.needs_manual:
            causes = persist.root_causes(case)
            case.attribution = attribution.attribute(deps.git, deps.prs, deps.config.repo,
                                                     deps.layout.readonly_worktree(), work.commit, causes)
            if case.attribution.note:
                case.notes.append(case.attribution.note)
            hit = (case.evidence.output or {}).get("tradeoffHit")
            if tradeoff_valid(deps.conn, hit):
                case.tradeoff_hit = hit

    def _finish(self, work: _Work, case: TriageCase) -> TriageItem:
        deps = self.deps
        run, problem = work.run, case.problem
        if case.merged_into is not None:
            reason = f"与 {case.merged_into} 同一根因，已并入"
            if not self.output_mode:
                persist.merge(deps.conn, deps.layout, deps.clock, deps.config, run_id=run.id, problem_id=problem.id,
                              target=case.merged_into, reason=reason, handled=case.handled)
            return self._write(run, problem, handoff_outputs(case, None, reason, work.commit), None,
                               attempt=case.attempt, merged_into=case.merged_into)
        decision = self._decide(case)
        reason = self._reason(case, decision)
        if not self.output_mode:
            record = persist.triage_record(case, decision, reason, work.commit, run.id, deps.clock)
            persist.commit(deps.conn, deps.layout, deps.clock, deps.config, run_id=run.id, problem_id=problem.id,
                           event=ProblemEvent.TRIAGED, context=decision.context, reason=reason, record=record,
                           score_rows=persist.score_records(case, run.id, deps.clock), handled=case.handled)
        passed = sum(1 for item in case.evidence.items if item.result.value == "pass") if case.evidence else 0
        work.tracer.event("gate", decision=decision.disposition.value, reason=reason,
                          score={"passed": passed, "items": len(case.evidence.items) if case.evidence else 0},
                          attributes={"problemId": problem.id})
        return self._write(run, problem, handoff_outputs(case, decision, reason, work.commit), decision,
                           attempt=case.attempt)

    def _decide(self, case: TriageCase) -> Decision:
        if case.verdict is None or case.rating is None:
            raise ValueError(f"问题 {case.problem.id} 还没有判定与评级")
        output = case.evidence.output if case.evidence is not None else None
        assessed = persist.assessment(case)
        return disposition.decide(
            self.deps.config, case.problem, verdict=case.verdict, severity=case.rating.severity,
            tier=case.rating.tier, worth=WorthRecommendation(assessed["worth"]) if assessed else None,
            fixed_on_main=bool(output and output.get("fixedOnMain")) and case.verdict in CONFIRMING,
            tradeoff_hit=case.tradeoff_hit is not None, needs_manual=case.needs_manual,
            refuter_verdict=case.refuter_verdict, estimated_files=case.rating.estimated_files,
            observed_before=self._observed_before(case.problem.id))

    def _observed_before(self, problem_id: str) -> bool:
        """上一次分诊的处理标签为观察(本次分诊的结论还没有写入)。"""
        previous = triage.latest(self.deps.conn, problem_id)
        return previous is not None and previous.result.treatment is Treatment.OBSERVE

    def _reason(self, case: TriageCase, decision: Decision) -> str:
        outputs = case.outputs
        evidence_part = outputs.get("evidence") or {}
        parts = [f"判定为{case.verdict.label if case.verdict else '无'}"]
        if evidence_part.get("trigger"):
            parts.append(f"触发条件：{evidence_part['trigger']}")
        source = evidence_part.get("sourceOfPhenomenon")
        if source:
            parts.append(f"现象来源：{source['explanation']}")
        missing = [item["item"] for item in outputs.get("missingInfo") or []]
        if missing and case.verdict is Verdict.INSUFFICIENT:
            parts.append(f"缺少：{'、'.join(missing)}")
        if case.tradeoff_hit:
            parts.append(f"命中已接受的取舍 {case.tradeoff_hit}")
        if decision.treatment is not None:
            parts.append(f"处理标签为{decision.treatment.label}")
        parts.append(f"去向为{decision.disposition.label}")
        return "；".join([*parts, *case.notes])

    # 交接文档与发现报告

    def _envelope(self, run: Run, problem_id: str, outputs: dict[str, Any], status: HandoffStatus,
                  next_action: str, reason: str | None = None) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schemaVersion": versions.current(ENVELOPE), "runId": run.id, "stage": STAGE.value,
            "subject": {"type": "problem", "id": problem_id}, "status": status.value, "inputsRef": {},
            "outputs": outputs, "nextAction": next_action, "createdAt": format_iso(self.deps.clock.now()),
        }
        if reason is not None:
            document["blockedReason"] = reason
        return document

    def _write(self, run: Run, problem: Problem, outputs: dict[str, Any], decision: Decision | None, *,
               attempt: int, merged_into: str | None = None) -> TriageItem:
        deps = self.deps
        conn = None if self.output_mode else deps.conn
        if decision is None:
            document = self._envelope(run, problem.id, outputs, HandoffStatus.OK, f"已并入 {merged_into}")
        else:
            blocked = decision.disposition is Disposition.MANUAL_QUEUE
            document = self._envelope(run, problem.id, outputs, HandoffStatus.BLOCKED if blocked else HandoffStatus.OK,
                                      NEXT_ACTIONS[decision.disposition], outputs["reason"] if blocked else None)
        written = handoff_files.write(deps.layout, document, deps.clock, conn=conn, attempt=attempt)
        report = None
        if decision is not None:
            path = deps.layout.output_finding(problem.id) if self.output_mode else deps.layout.finding(problem.id)
            report = findings.write(path, document, deps.zone)
        return TriageItem(problem.id, problem.title, HandoffStatus(document["status"]),
                          Verdict(outputs["verdict"]) if outputs["verdict"] else None,
                          Severity(outputs["severity"]) if outputs["severity"] else None,
                          decision.disposition if decision else None, written.path, report, merged_into,
                          outputs["reason"])

    def _failed(self, run: Run, problem: Problem, error: Exception) -> TriageItem:
        detail = "".join(traceback.format_exception_only(type(error), error)).strip()
        frames = traceback.extract_tb(error.__traceback__)[-3:]
        where = "；".join(f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}" for frame in frames)
        reason = f"{detail}(位置 {where})" if where else detail
        document = self._envelope(run, problem.id, {}, HandoffStatus.FAILED,
                                  "查看失败原因后执行 tightrein problem retriage 重新分诊", reason)
        written = handoff_files.write(self.deps.layout, document, self.deps.clock,
                                      conn=None if self.output_mode else self.deps.conn)
        return TriageItem(problem.id, problem.title, HandoffStatus.FAILED, handoff=written.path, reason=reason)

    def _previous_outputs(self, problem_id: str) -> dict[str, Any] | None:
        record = handoffs.get(self.deps.conn, STAGE, problem_id)
        if record is None:
            return None
        document = handoff_files.read(self.deps.layout.root / record.path)
        return document["outputs"] if document["status"] != HandoffStatus.FAILED.value else None

    def _overridden_outputs(self, problem: Problem, record: triage.TriageRecord, reason: str) -> dict[str, Any]:
        """改判沿用上一次分诊的主张与证据，替换判定、严重度、去向与理由；没有上一次时由问题重新组装主张。"""
        deps = self.deps
        result = record.result
        previous = self._previous_outputs(problem.id)
        if previous is None:
            found = signals.get_many(deps.conn, problems.signal_ids(deps.conn, problem.id))
            claim = claims.build(problem, found, [])
            previous = {"claim": claim.to_dict(), "complexity": None, "rootCauses": [], "introducedBy": [],
                        "flags": None, "evidence": None, "worth": None, "estimate": None, "taskType": None,
                        "sizeTier": None, "fixedOnMain": None, "tradeoffHit": None, "missingInfo": [],
                        "incidentalFindings": []}
        return {**previous, "problemId": problem.id, "verdict": result.verdict.value,
                "severity": None if result.severity is None else result.severity.value,
                "disposition": result.disposition.value, "reason": reason, "triageCommit": result.triage_commit,
                "refuterVerdict": None, "treatment": None if result.treatment is None else result.treatment.value,
                "labels": [], "mergedInto": None, "scores": [], "attempts": []}
