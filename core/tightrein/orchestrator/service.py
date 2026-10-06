"""编排层入口(architecture/09 3.1、3.4、3.7)：run、status、next、continue、find。

- run 在运行锁 data/run.lock 下执行(不等待，被占用时抛出 FileLockBusy；定时运行另在 schedule_state 记一条跳过)：
  中断恢复 → rules.STEPS(工作区接入中时只做接入检查) → 运行摘要与每日汇总 → 本机通知 → loop 运行记录结束；
  暂停时不发起运行(抛出 pause.Paused)，运行中途暂停或预算到达时在当前步骤完成后停下；
- continue 开始时执行中断恢复(不持有运行锁，只接管锁持有进程已退出的运行)，产生一条 loop 运行记录，按状态表逐步
  调用模块(execute)，停在关口、终点或失败处；
- execute 把 domain/next_step.py 的命令映射到模块调用，只处理一个对象。编排不判断对象该如何处理。
"""

from __future__ import annotations

import socket
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta, tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, local_date
from tightrein.domain.enums import HandoffStatus, OperationKind, OperationStatus, RunStage, RunStatus, Stage
from tightrein.domain.next_step import NextStep
from tightrein.domain.run import Run
from tightrein.observability.events import EventLog
from tightrein.observability.notify import Notifier, NotifyResult
from tightrein.orchestrator import breaker, inbox, pause, recovery, resume, runlog, schedule, summary
from tightrein.orchestrator.modules import Modules
from tightrein.orchestrator.resume import ContinueReport, NextView, StepResult, SubjectRef
from tightrein.orchestrator.rules import RunContext, RunRequest, StepOutcome, preview_steps, run_steps
from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.pipeline.aggregate.service import AggregateRequest
from tightrein.pipeline.fix.service import ResumePoint
from tightrein.pipeline.triage.service import TriageRequest
from tightrein.runner import limits
from tightrein.store import locks
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.locks import FileLockBusy
from tightrein.store.repos import issues, pending_operations, runs, schedule_state

RECOVERY_STEP = "recovery"
ONBOARDING_STEP = "onboarding"
QUIET_EVENT_STEP = "deployments"  # 每次事件运行都执行的部署检测；只有它执行时不重写每日汇总
TICK_PAUSED, TICK_IDLE, TICK_FULL, TICK_EVENTS = "paused", "idle", "full", "events"


@dataclass
class RunReport:
    run: Run
    outputs: dict[str, Any]
    handoff: Path
    report: Path
    notification: NotifyResult | None = None


@dataclass
class TickResult:
    kind: str  # paused、idle、full、events
    message: str
    report: RunReport | None = None


@dataclass
class ContinueResult:
    run: Run
    report: ContinueReport
    refs: list[SubjectRef] = field(default_factory=list)


def _status_of(result: Any) -> HandoffStatus:
    return result.status if result.status is not None else HandoffStatus.OK


class Orchestrator:
    def __init__(self, modules: Modules, *, layout: WorkspaceLayout, conn: sqlite3.Connection,
                 config: ProjectConfig, clock: Clock, events: EventLog, notifier: Notifier | None,
                 zone: tzinfo | None, alive: Callable[[int], bool], host: str | None = None,
                 reroutes: Callable[[], Sequence[str]] | None = None, pause_flag: Path | None = None,
                 onboarding: Any = None) -> None:
        """pause_flag 为全局暂停的标记文件；onboarding 为接入流程(orchestrator/onboarding/service.Onboarding)。"""
        self.modules = modules
        self.layout = layout
        self.conn = conn
        self.config = config
        self.clock = clock
        self.events = events
        self.notifier = notifier
        self.zone = zone
        self.alive = alive
        self.host = host or socket.gethostname()
        self.pacer = runlog.Pacer(clock, modules.wait_until)
        self.reroutes = reroutes
        self.pause_flag = pause_flag
        self.onboarding = onboarding

    def paused(self) -> str | None:
        return pause.reason(self.conn, self.pause_flag)

    # run

    def run(self, request: RunRequest) -> RunReport:
        halted = self.paused()
        if halted is not None:
            raise pause.Paused(halted)
        try:
            with locks.file_lock(self.layout.run_lock(), wait=False):
                return self._run(request)
        except FileLockBusy:
            if request.scheduled:
                schedule.skipped(self.conn, self.clock.now())
            raise

    def preview(self, request: RunRequest) -> list[StepOutcome]:
        """run --dry-run：不取运行锁、不开始运行、不写任何记录，按当前状态列出各步会不会执行及理由。"""
        ctx = RunContext(self.modules, self.conn, self.layout, self.config, self.clock, self.zone, None, request,
                         self.pacer, advance=self._advance, paused=self.paused,
                         budget_baseline=limits.GlobalBudget(self.conn, self.clock, limits.BudgetLimits.from_config(
                             self.config), self.zone).total())
        if self.onboarding is not None and self.onboarding.active():
            return [StepOutcome(2, ONBOARDING_STEP, True, "接入中：只做接入检查")]
        preview_steps(ctx, start=2)
        return ctx.outcomes

    def _run(self, request: RunRequest) -> RunReport:
        loop = runlog.begin(self.conn, self.clock, self.events, self._ttl())
        self.pacer.mark(RunStage.LOOP.value, loop.run.started_at)
        ctx = RunContext(self.modules, self.conn, self.layout, self.config, self.clock, self.zone, loop, request,
                         self.pacer, advance=self._advance, paused=self.paused,
                         budget_baseline=limits.GlobalBudget(self.conn, self.clock, limits.BudgetLimits.from_config(
                             self.config), self.zone).total())
        found = recovery.recover(self.conn, self.clock, self.events, alive=self.alive, host=self.host,
                                 current=loop.id)
        reason = f"接管 {len(found)} 个中断的运行" if found else "没有中断的运行"
        runlog.gate(loop, RECOVERY_STEP, True, reason)
        ctx.outcomes.append(StepOutcome(1, RECOVERY_STEP, True, reason, [item.run_id for item in found],
                                        RunStatus.OK, 0))
        if self.onboarding is not None and self.onboarding.active():
            started = self.clock.now()
            note = self.onboarding.check().summary
            ctx.outcomes.append(StepOutcome(2, ONBOARDING_STEP, True, note, [], RunStatus.OK,
                                            round((self.clock.now() - started).total_seconds() * 1000)))
        else:
            run_steps(ctx, start=2)
        anomalies = [*ctx.anomalies, *({"source": "events", "reason": failure.reason, "log": str(failure.path)}
                                       for failure in self.events.failures)]
        children = [run.id for run in runs.find(self.conn, parent_run_id=loop.id)]
        values = summary.outputs(self.conn, self.layout, self.config, ctx.outcomes, anomalies, children,
                                 list(self.reroutes()) if self.reroutes is not None else [], ctx.halted)
        report_path = self.layout.daily_report(local_date(self.clock.now(), self.zone))
        notification = summary.notify(self.notifier, self.config.name, loop.id, values, report_path, ctx.health,
                                      scheduled=request.scheduled)
        failed_notice = summary.failed_notice(notification)
        if failed_notice is not None:
            values["anomalies"].append(failed_notice)
        quiet = request.events and not any(step.executed for step in ctx.outcomes
                                           if step.name not in (RECOVERY_STEP, QUIET_EVENT_STEP))
        handoff, report = summary.write(self.layout, self.conn, self.clock, loop, values,
                                        language=self.config.language, zone=self.zone,
                                        onboarding=self.onboarding.digest_lines() if self.onboarding else (),
                                        daily=not quiet)
        failed = any(step.status is RunStatus.FAILED for step in ctx.outcomes)
        run = runlog.finish(self.conn, self.clock, loop, runlog.final_status(failed=failed,
                                                                             waiting=bool(values["waiting"])))
        return RunReport(run, values, handoff, report, notification)

    def tick(self) -> TickResult:
        """launchd 定时唤醒：暂停时什么都不做；工作日 schedule.runAt 到期时做完整运行(接入中的工作区只做接入检查)，
        其余时刻只做事件运行(新提交的增量巡检、新部署的部署后确认等)；接入中的工作区不做事件运行。"""
        halted = self.paused()
        if halted is not None:
            return TickResult(TICK_PAUSED, halted)
        now = self.clock.now()
        due = schedule.run_due(self.config, self.conn, now, self.zone)
        if due is None and self.onboarding is not None and self.onboarding.active():
            return TickResult(TICK_IDLE, "接入中：只在固定时刻做接入检查")
        report = self.run(RunRequest(scheduled=True, events=due is None))
        if due is not None:
            schedule.started(self.conn, due.name, now, report.run.id, due.missed)
            schedule.ended(self.conn, due.name, self.clock.now(), report.run.status)
        return TickResult(TICK_FULL if due is not None else TICK_EVENTS, report.outputs["conclusion"], report)

    # 全局状态

    def status(self) -> dict[str, Any]:
        loops = runs.find(self.conn, stage=RunStage.LOOP)
        last = loops[-1] if loops else None
        return {
            "waiting": inbox.items(self.conn, self.layout),
            "paused": self.paused(),
            "onboarding": self.onboarding.status_line() if self.onboarding is not None else None,
            "lastRun": None if last is None else {
                "runId": last.id, "status": last.status.value,
                "report": self.layout.relative(self.layout.daily_report(local_date(last.started_at, self.zone)))},
            "schedule": [{"task": state.task, "lastStartedAt": state.last_started_at.isoformat()
                          if state.last_started_at else None,
                          "lastStatus": state.last_status.value if state.last_status else None,
                          "missed": state.missed_count} for state in schedule_state.all_states(self.conn)],
        }

    # 对象

    def refs(self, tokens: Sequence[str], selectors: Sequence[str] = ()) -> list[SubjectRef]:
        found = [resume.identify(self.conn, token) for token in tokens]
        return list(dict.fromkeys([*found, *resume.select(self.conn, selectors)]))

    def next(self, tokens: Sequence[str]) -> list[NextView]:
        return [resume.next_view(self.conn, ref) for ref in self.refs(tokens)]

    def find(self, text: str, *, since: date | None = None, until: date | None = None,
             kind: str | None = None) -> list[resume.Candidate]:
        return resume.find(self.conn, text, since=since, until=until, kind=kind,
                           limit=int(self.config.get("loop.findLimit")), zone=self.zone)

    def continue_(self, tokens: Sequence[str], selectors: Sequence[str] = (), *, until: Stage | None = None,
                  from_: Stage | None = None, interactive: bool = False,
                  confirm: Callable[[str], bool] | None = None) -> ContinueResult:
        refs = self.refs(tokens, selectors)
        if not refs:
            raise ValueError("没有给出对象，也没有选择器匹配到对象")
        recovery.recover(self.conn, self.clock, self.events, alive=self.alive, host=self.host)
        loop = runlog.begin(self.conn, self.clock, self.events, self._ttl())
        self.pacer.mark(RunStage.LOOP.value, loop.run.started_at)
        before = runlog.known_runs(self.conn)
        if from_ is not None:
            restarted: list[SubjectRef] = []
            for ref in refs:
                restarted += resume.restart(self.conn, self.layout, self.clock, self.config, ref, from_,
                                            self._retriage, self.zone)
            refs = list(dict.fromkeys(restarted))
        resumer = resume.Resumer(self.conn, self.clock, self.execute, lock_ttl=self._ttl(), until=until,
                                 interactive=interactive, confirm=confirm,
                                 unattended=gates.auto(self.config, Gate.FIX_SESSION))
        report = resumer.continue_(refs)
        runlog.adopt(self.conn, loop, before)
        status = runlog.final_status(failed=report.failed, waiting=bool(report.gates))
        return ContinueResult(runlog.finish(self.conn, self.clock, loop, status), report, refs)

    def _advance(self, refs: Sequence[SubjectRef], stopping: Callable[[], str | None] | None = None) -> ContinueReport:
        """run 中的无人值守推进(gates.fix-session 为 auto)：对已放行的 Issue 按状态表续跑到 release，不等待终端确认；
        熔断计数(orchestrator/breaker.py)，暂停或预算到达后不再开始下一个对象。"""
        fuse = breaker.Breaker(self.conn, self.clock, self.config, lambda issue_id, reason: self.modules.fix().hold(
            issue_id, reason))
        resumer = resume.Resumer(self.conn, self.clock, self.execute, lock_ttl=self._ttl(), until=Stage.RELEASE,
                                 unattended=True, observe=fuse.observe, stopping=stopping)
        return resumer.continue_(refs)

    def _ttl(self) -> timedelta:
        return timedelta(minutes=int(self.config.get("loop.objectLockMinutes")))

    def _retriage(self, problem_id: str) -> None:
        self._call(RunStage.TRIAGE.value,
                   lambda: self.modules.triage().retriage(problem_id, overrides=self.modules.overrides))

    def _call(self, key: str, action: Callable[[], Any]) -> Any:
        return self.pacer.call(key, action)

    # 单步执行

    def _gate(self, operation: str | None) -> str | None:
        if operation is None:
            return None
        record = pending_operations.get(self.conn, operation)
        return resume.FIX_PLAN if record is not None and record.kind is OperationKind.FIX_PLAN else None

    def execute(self, target: SubjectRef, step: NextStep) -> StepResult:
        command = step.command
        modules = self.modules
        if command == "aggregate":
            result = self._call(RunStage.AGGREGATE.value, lambda: modules.aggregate().run(AggregateRequest()))
            return StepResult(_status_of(result), result.message or "聚合完成")
        if command == "triage":
            outcome = self._call(RunStage.TRIAGE.value,
                                 lambda: modules.triage().run(TriageRequest(select=(target.id,),
                                                                            overrides=modules.overrides)))
            if not outcome.items:
                reason = "；".join(text for _, text in outcome.skipped) or outcome.message or "没有分诊"
                return StepResult(HandoffStatus.BLOCKED, reason)
            item = outcome.items[0]
            return StepResult(item.status, item.reason or outcome.summary or f"{item.problem_id} 已分诊")
        if command == "issue create":
            outcome = self._call(RunStage.ISSUE.value, lambda: modules.issue().create((target.id,)))
            if not outcome.items:
                return StepResult(HandoffStatus.BLOCKED, "；".join(text for _, text in outcome.skipped) or "没有创建")
            item = outcome.items[0]
            return StepResult(item.status, item.reason or f"Issue {item.issue_id}：{item.action}")
        if command == "fix start":
            return self._fix(target.id)
        if command == "verify local":
            result = self._call(RunStage.VERIFY.value, lambda: modules.verify().local(target.id))
            if (result.status is HandoffStatus.BLOCKED and gates.auto(self.config, Gate.FIX_SESSION)
                    and modules.fix().resume_point(target.id) is ResumePoint.REVIEW):
                reviewed = self._fix_unattended(target.id)  # 合并 main 后改动与评审时不同：重新评审后再验证
                if reviewed.status is not HandoffStatus.OK:
                    return reviewed
                result = self._call(RunStage.VERIFY.value, lambda: modules.verify().local(target.id))
            return StepResult(result.status, result.message)
        if command == "verify staging":
            result = self._call(RunStage.VERIFY.value, lambda: modules.verify().staging(target.id))
            gate = resume.AWAITING_DEPLOY if result.status is HandoffStatus.BLOCKED else None
            return StepResult(result.status, result.message, gate=gate)
        if command == "release":
            result = self._call(RunStage.RELEASE.value, lambda: modules.release().release(target.id))
            return StepResult(result.status, result.message, result.operation, self._gate(result.operation))
        raise ValueError(f"状态表中的命令 {command} 没有对应的模块调用")

    def _fix(self, issue_id: str) -> StepResult:
        """终端中启动修复会话；会话结束且修复已完成时执行 fix done。gates.fix-session 为 auto 时改为无人值守修复。"""
        if gates.auto(self.config, Gate.FIX_SESSION):
            return self._fix_unattended(issue_id)
        fix = self.modules.fix()
        result = self._call(RunStage.FIX.value, lambda: fix.start(issue_id))
        if result.status is not HandoffStatus.OK:
            return StepResult(result.status, result.message, result.operation, self._gate(result.operation))
        if fix.resume_point(issue_id) is not ResumePoint.DONE:
            return StepResult(HandoffStatus.BLOCKED, f"{result.message}；修复尚未完成，下一步 fix "
                              f"{fix.resume_point(issue_id).value}", gate=resume.INTERACTIVE_FIX)
        done = self._call(RunStage.FIX.value, lambda: fix.done(issue_id))
        return StepResult(done.status, done.message, done.operation, self._gate(done.operation))


    def _fix_unattended(self, issue_id: str) -> StepResult:
        """不启动交互会话，按续接点依次执行 fix plan、自动确认、fix apply、fix done(都是非交互任务)。
        某一步之后续接点没有前进即停下：停在待确认操作上的(计划需要用户确认、建分支等待确认)照常作为关口；其余(守卫违规、
        检查不通过且重试用尽、复现不符等)经 fix.stop_unattended 转人工并在 GitHub 镜像写明原因。"""
        fix = self.modules.fix()
        issue = issues.get(self.conn, issue_id).issue
        waiting = fix.waiting_on(issue)
        if waiting is not None:
            return StepResult(HandoffStatus.BLOCKED, waiting)
        if not fix.worktree(issue_id).is_dir():
            prepared = self._call(RunStage.FIX.value, lambda: fix.prepare(issue_id))
            if prepared.status is not HandoffStatus.OK or not fix.worktree(issue_id).is_dir():
                return self._unattended_stop(issue_id, prepared.status, prepared.message, prepared.operation)
        started = self._call(RunStage.FIX.value, lambda: fix.start(issue_id, here=True))
        if started.status is not HandoffStatus.OK:
            return self._unattended_stop(issue_id, started.status, started.message, started.operation)
        while True:
            point = fix.resume_point(issue_id)
            if point is ResumePoint.DONE:
                done = self._call(RunStage.FIX.value, lambda: fix.done(issue_id))
                if done.status is HandoffStatus.OK:
                    return StepResult(HandoffStatus.OK, done.message)
                return self._unattended_stop(issue_id, done.status, done.message, done.operation)
            status, message, operation = self._unattended_action(issue_id, point)
            if status is HandoffStatus.FAILED or fix.resume_point(issue_id) is point:
                return self._unattended_stop(issue_id, status, message, operation)

    def _unattended_action(self, issue_id: str, point: ResumePoint) -> tuple[HandoffStatus, str, str | None]:
        fix = self.modules.fix()
        if point is ResumePoint.PLAN:
            result = self._call(RunStage.FIX.value, lambda: fix.plan(issue_id))
        elif point is ResumePoint.CONFIRM:
            result = fix.auto_confirm(issue_id)
            if result is None:
                return HandoffStatus.BLOCKED, f"修复计划需要用户确认：fix confirm {issue_id}", None
        else:
            review_only = point is ResumePoint.REVIEW
            result = self._call(RunStage.FIX.value, lambda: fix.apply(issue_id, review_only=review_only))
        return result.status, result.message, result.operation

    def _unattended_stop(self, issue_id: str, status: HandoffStatus, message: str,
                         operation: str | None) -> StepResult:
        record = pending_operations.get(self.conn, operation) if operation is not None else None
        if record is not None and record.status is OperationStatus.PENDING:
            return StepResult(HandoffStatus.BLOCKED, message, operation, self._gate(operation))
        self.modules.fix().stop_unattended(issue_id, message)
        stopped = status if status is not HandoffStatus.OK else HandoffStatus.BLOCKED
        return StepResult(stopped, f"无人值守修复停下：{message}")
