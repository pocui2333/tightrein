"""一次 run 的固定步骤与触发条件(architecture/09 3.1，design 9.8)。

触发条件只读状态表与时间表，判断「有没有需要调用的对象」，不判断对象该如何处理。每一步包在独立的错误边界中：
抛出异常只记为该步骤失败并写入异常，后续步骤照常执行。需要用户的事项(放行 Issue、确认修复计划、确认 git 写操作、
审核 PR)不在 run 中推进，由运行摘要列出。gates.fix-session 为 auto 时 unattended 步骤把已放行的 Issue 以无人值守方式推进
(修复不启动交互会话，见 Orchestrator._fix_unattended)，到 release 为止。
链选择(run --select <链>)只执行链上的模块步骤；中断恢复与健康检查照常执行。事件运行(tick 在固定时刻之外，
RunRequest.events)只执行 EVENT_STEPS：部署检测与部署后浅跑、新提交的增量巡检、到期的平台来源与项目探针、PR 与
部署跟踪、部署后确认。sources 步骤在摘要中列出未启用的采集方法。
每一步之前检查暂停与预算(RunContext.halting)：暂停时其余步骤都不再执行，预算到达时其余调用模型的步骤不再执行，原因写进摘要。
无人值守推进按处理标签与严重度排序，同时处于修复阶段的 Issue 不超过 loop.maxActiveFixes。
同一环节的运行编号精确到秒：模块调用经 RunContext.call(runlog.Pacer)，保证与上一次同类调用不在同一秒。
"""

from __future__ import annotations

import shlex
import sqlite3
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, local_date
from tightrein.domain.enums import (
    DeploymentStatus,
    HandoffStatus,
    IssuePhase,
    IssueStatus,
    ProbeLevel,
    RunStage,
    RunStatus,
    Stage,
)
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.issue import Issue, in_phase
from tightrein.domain.triage import urgency_key
from tightrein.orchestrator import runlog, schedule
from tightrein.orchestrator.modules import Modules
from tightrein.orchestrator.resume import ISSUE, ContinueReport, SubjectRef
from tightrein.orchestrator.runlog import LoopRun, Pacer
from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.fix.steps import split
from tightrein.pipeline.aggregate.service import AggregateRequest
from tightrein.pipeline.collect.service import CollectRequest
from tightrein.pipeline.issue.steps import github_comments
from tightrein.pipeline.issue.steps import select as issue_select
from tightrein.pipeline.triage.service import TriageRequest
from tightrein.pipeline.triage.steps import select as triage_select
from tightrein.sources.common import target as common_target
from tightrein.sources.project_probes import registry as probe_registry
from tightrein.runner import limits
from tightrein.vcs.errors import VcsError
from tightrein.store import idempotency
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, issues, runs
from tightrein.pipeline.common.deploys import UNCONFIGURED as DEPLOY_UNCONFIGURED

ALWAYS = "always"
RETENTION_KEY = "retention:{day}"
FAILED_RUN = frozenset({RunStatus.FAILED, RunStatus.INTERRUPTED})
UNATTENDED_STATUSES = (IssueStatus.TODO, IssueStatus.IN_PROGRESS)
# 编排的 sources 步骤按间隔运行的采集方法(api-fuzz 与静态巡检由部署、新提交与定时任务触发)
SCHEDULED_SOURCES = (ProbeKind.PLATFORM_ERRORS, ProbeKind.ACCESS_LOG, ProbeKind.ALERTS, ProbeKind.PROJECT_PROBE)


@dataclass(frozen=True)
class RunRequest:
    """chain 为空时执行全部步骤；给出时只执行链上的模块(15.3)，subject 与 probe 限定对象与探针。"""

    scheduled: bool = False
    chain: tuple[Stage, ...] | None = None
    subject: str | None = None
    probe: ProbeKind | None = None
    level: ProbeLevel | None = None
    events: bool = False


@dataclass
class StepOutcome:
    order: int
    name: str
    executed: bool
    reason: str
    run_ids: list[str] = field(default_factory=list)
    status: RunStatus | None = None
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"order": self.order, "name": self.name, "executed": self.executed, "reason": self.reason,
                "runId": self.run_ids[0] if self.run_ids else None,
                "status": self.status.value if self.status else None, "durationMs": self.duration_ms}


@dataclass
class RunContext:
    modules: Modules
    conn: sqlite3.Connection
    layout: WorkspaceLayout
    config: ProjectConfig
    clock: Clock
    zone: tzinfo | None
    loop: LoopRun | None  # 预览(run --dry-run)时为空：不开始运行，只判断触发条件
    request: RunRequest
    pacer: Pacer
    outcomes: list[StepOutcome] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    health: list[dict[str, Any]] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    advance: Callable[..., ContinueReport] | None = None  # 无人值守推进(Orchestrator._advance)
    paused: Callable[[], str | None] | None = None  # 暂停的原因(orchestrator/pause.py)
    budget_baseline: float | None = None  # 运行开始时全部环节的费用总额，每次运行的预算按它计算
    halted: dict[str, str] = field(default_factory=dict)  # 停下的原因：pause、budget

    def halting(self, models: bool = True) -> str | None:
        """暂停时总是停下；预算到达时只停调用模型的步骤。原因记入 halted，供摘要列出。"""
        reason = self.paused() if self.paused is not None else None
        if reason is not None:
            self.halted.setdefault("pause", reason)
            return reason
        if not models:
            return None
        found = limits.GlobalBudget(self.conn, self.clock, limits.BudgetLimits.from_config(self.config),
                                    self.zone).exceeded(self.budget_baseline)
        if found is not None:
            self.halted.setdefault("budget", found)
        return found

    def call(self, key: str, action: Callable[[], Any]) -> Any:
        return self.pacer.call(key, action)

    def anomaly(self, source: str, reason: str, log: str | None = None) -> None:
        self.anomalies.append({"source": source, "reason": reason, "log": log})


Trigger = Callable[[RunContext], str | None]
Action = Callable[[RunContext], None]


@dataclass(frozen=True)
class Step:
    name: str
    module: Stage | str | None
    trigger: Trigger
    action: Action
    models: bool = False  # 会调用模型(预算到达时不再执行)


# 触发条件：返回执行的理由，None 表示跳过

def _always(ctx: RunContext) -> str | None:
    return "总是执行"


def _deployed_commit(ctx: RunContext) -> str | None:
    commit = common_target.latest_release(ctx.conn)
    if commit is None:
        return None
    wanted = {ProbeKind(item["probe"]) for item in ctx.config.data.get("schedule", {}).get("onDeploy", [])}
    done = {run.probe for run in runs.find(ctx.conn, stage=RunStage.COLLECT) if run.target_commit == commit}
    return commit if wanted - done else None


def _on_deploy(ctx: RunContext) -> str | None:
    commit = _deployed_commit(ctx)
    return None if commit is None else f"部署 {commit[:12]} 尚未浅跑"


def _scheduled(ctx: RunContext) -> str | None:
    due = schedule.due_tasks(ctx.config, ctx.conn, ctx.clock.now(), ctx.zone)
    return f"到期任务：{'、'.join(task.name for task in due)}" if due else None


def _chain_collect(ctx: RunContext) -> str | None:
    return f"链选择：collect --probe {ctx.request.probe.value}" if ctx.request.probe is not None else None


def _chain_aggregate(ctx: RunContext) -> str | None:
    return "链选择：aggregate"


def _triage(ctx: RunContext) -> str | None:
    if ctx.request.subject is not None:
        return f"链选择：{ctx.request.subject}"
    found = triage_select.pending(ctx.conn)
    return f"{len(found)} 个问题等待分诊" if found else None


def _issue(ctx: RunContext) -> str | None:
    if ctx.request.subject is not None:
        return f"链选择：{ctx.request.subject}"
    found = issue_select.pending(ctx.conn)
    return f"{len(found)} 个问题等待创建 Issue" if found else None


def _tracked(ctx: RunContext) -> str | None:
    found = [record for record in issues.find(ctx.conn) if record.issue.status is IssueStatus.PENDING_MERGE
             or in_phase(record.issue, IssuePhase.DEPLOY_CHECK)]
    return f"{len(found)} 个 Issue 的 PR 或部署待跟踪" if found else None


def deployed_issues(ctx: RunContext) -> list[str]:
    """已合并(完成且等待部署后确认)、且 release track 记录了包含其合并提交的成功部署的 Issue。"""
    found = []
    for record in issues.find(ctx.conn, status=IssueStatus.DONE):
        if not in_phase(record.issue, IssuePhase.DEPLOY_CHECK):
            continue
        latest = stage_runs.latest_outputs(ctx.conn, ctx.layout, RunStage.RELEASE, record.issue.id)
        deployments = latest[1].get("deployments", []) if latest is not None else []
        if any(item["status"] == DeploymentStatus.SUCCEEDED.value for item in deployments):
            found.append(record.issue.id)
    return found


def _staging(ctx: RunContext) -> str | None:
    found = deployed_issues(ctx)
    return f"已部署待验证：{'、'.join(found)}" if found else None


def unattended_issues(ctx: RunContext) -> list[Issue]:
    """gates.fix-session 为 auto 时由 run 推进的 Issue：已放行(待修与进行中)、排在前面的子任务已合并；按处理标签与
    严重度排序(立即修先于排期修)。处于修复阶段的计入并发，待修的只在 loop.maxActiveFixes 有空位时开始；已过修复
    阶段的照常推进。待决定的 Issue(关卡 needs-decision)不在其中。"""
    found = [record.issue for status in UNATTENDED_STATUSES for record in issues.find(ctx.conn, status=status)]
    ready = sorted((issue for issue in found if split.waiting_on(ctx.conn, issue) is None),
                   key=lambda issue: urgency_key(issue.treatment, issue.severity))
    slots = int(ctx.config.get("loop.maxActiveFixes")) - sum(in_phase(issue, IssuePhase.FIX) for issue in ready)
    chosen = []
    for issue in ready:
        if issue.status is IssueStatus.TODO:
            if slots <= 0:
                continue
            slots -= 1
        chosen.append(issue)
    return chosen


def _unattended(ctx: RunContext) -> str | None:
    if not gates.auto(ctx.config, Gate.FIX_SESSION) or ctx.advance is None:
        return None
    found = unattended_issues(ctx)
    return f"{len(found)} 个已放行的 Issue 无人值守推进" if found else None


def _unattended_action(ctx: RunContext) -> None:
    """还没有修复分支的用户需求与拆分出的后续子任务先建分支(autonomy 视为已放行)，之后按状态表续跑到 release。"""
    refs = []
    for issue in unattended_issues(ctx):
        if issue.is_manual and issue.status is IssueStatus.TODO and issue.branch is None:
            prepared = ctx.call(RunStage.FIX.value, lambda issue_id=issue.id: ctx.modules.fix().prepare(issue_id))
            ctx.notes.append(f"Issue {issue.id} 建修复分支：{prepared.message}")
            if issues.get(ctx.conn, issue.id).issue.branch is None:
                continue
        refs.append(SubjectRef(ISSUE, issue.id))
    if not refs:
        return
    report = ctx.advance(refs, lambda: ctx.halting())
    for stop in report.stops:
        ctx.notes.append(f"Issue {stop.ref.id} 无人值守推进停在「{stop.reason}」")
        if stop.failed:
            ctx.anomaly("unattended", f"Issue {stop.ref.id}：{stop.reason}")


def _last_static_commit(ctx: RunContext) -> str | None:
    """最近一次正常结束的静态巡检的目标 commit；失败或中断的运行不算(下一次 tick 会重新触发)。"""
    found = [run for run in runs.find(ctx.conn, stage=RunStage.COLLECT) if run.probe is ProbeKind.STATIC
             and run.target_commit is not None and run.status not in (*FAILED_RUN, RunStatus.RUNNING)]
    return found[-1].target_commit if found else None


def _commit_patrol(ctx: RunContext) -> str | None:
    """主分支有新提交(与最近一次静态巡检的目标 commit 不同)；从未巡检过的项目不触发(第一次是基线审查，由用户发起)。"""
    last = _last_static_commit(ctx)
    if last is None:
        return None
    try:
        head = ctx.modules.main_head()
    except VcsError as error:
        ctx.anomaly("commit-patrol", f"读取主分支的最新提交失败：{error}")
        return None
    if head is None or head == last:
        return None
    ctx.notes.append(f"主分支新提交 {head[:12]}")
    return f"主分支新提交 {head[:12]}(上次巡检 {last[:12]})"


def _commit_patrol_action(ctx: RunContext) -> None:
    head = ctx.modules.main_head()
    if head is None:
        return
    _aggregate_runs(ctx, _collect(ctx, CollectRequest(ProbeKind.STATIC, level=ProbeLevel.INCREMENTAL, commit=head)))


def _source_due(ctx: RunContext, probe: ProbeKind) -> bool:
    """平台来源按 sources.<方法>.every 到期；项目探针由 collect 按各探针自己的间隔挑选，有任一到期即运行。"""
    now = ctx.clock.now()
    if probe is ProbeKind.PROJECT_PROBE:
        return bool(probe_registry.due(ctx.conn, probe_registry.registered(ctx.config), now))
    every = probe_registry.interval(str(ctx.config.get(f"sources.{probe.value}.every")))
    found = [run for run in runs.find(ctx.conn, stage=RunStage.COLLECT, probe=probe)
             if run.status not in (*FAILED_RUN, RunStatus.RUNNING)]
    return not found or now - found[-1].started_at >= every


def _due_sources(ctx: RunContext) -> list[ProbeKind]:
    disabled = ctx.modules.disabled_sources()
    return [probe for probe in SCHEDULED_SOURCES if probe.value not in disabled and _source_due(ctx, probe)]


def _sources(ctx: RunContext) -> str | None:
    disabled = ctx.modules.disabled_sources()
    off = [name for name in (probe.value for probe in SCHEDULED_SOURCES) if name in disabled]
    if off:
        ctx.notes.append("未启用的采集方法：" + "；".join(f"{name}({disabled[name]})" for name in off))
    due = _due_sources(ctx)
    return f"到期的采集：{'、'.join(probe.value for probe in due)}" if due else None


def _sources_action(ctx: RunContext) -> None:
    collected: list[str] = []
    for probe in _due_sources(ctx):
        collected += _collect(ctx, CollectRequest(probe))
    _aggregate_runs(ctx, collected)


def _mirror(ctx: RunContext) -> str | None:
    return "issues.tracker 为 github" if github_comments.enabled(ctx.config) else None


def _weekly(ctx: RunContext) -> str | None:
    due = schedule.weekly_due(ctx.config, ctx.conn, ctx.clock.now(), ctx.zone)
    return None if due is None else "本周第一个工作日的周任务"


def _retention_key(ctx: RunContext) -> str:
    return RETENTION_KEY.format(day=local_date(ctx.clock.now(), ctx.zone).isoformat())


def _retention(ctx: RunContext) -> str | None:
    record = idempotency.get(ctx.conn, _retention_key(ctx))
    return None if record is not None and record.status == idempotency.DONE else "今天尚未清理"


# 动作

def _deployments(ctx: RunContext) -> None:
    if not ctx.modules.deploys().configured():
        ctx.notes.append(DEPLOY_UNCONFIGURED)
        return
    found = ctx.modules.collect().deployments()
    if found is not None:
        ctx.notes.append(f"检测到部署 {found.commit[:12]}({found.status.value})")


def _aggregate_runs(ctx: RunContext, run_ids: Sequence[str]) -> None:
    for run_id in run_ids:
        ctx.call(RunStage.AGGREGATE.value,
                 lambda run_id=run_id: ctx.modules.aggregate().run(AggregateRequest(select=(f"run:{run_id}",))))


def _collect(ctx: RunContext, request: CollectRequest) -> list[str]:
    key = f"{RunStage.COLLECT.value}-{request.probe.value}"
    result = ctx.call(key, lambda: ctx.modules.collect().run(request))
    found = [result.run.id] if result.run is not None else []
    ctx.collected += found
    return found


def _shallow(ctx: RunContext) -> None:
    commit = _deployed_commit(ctx)
    if commit is None:
        return
    ctx.modules.sync_readonly(commit)
    done = {run.probe for run in runs.find(ctx.conn, stage=RunStage.COLLECT) if run.target_commit == commit}
    collected: list[str] = []
    for item in ctx.config.data["schedule"]["onDeploy"]:
        probe = ProbeKind(item["probe"])
        if probe in done:
            continue
        level = ProbeLevel(item["level"]) if "level" in item else None
        collected += _collect(ctx, CollectRequest(probe, level=level, commit=commit))
    _aggregate_runs(ctx, collected)


def _tasks(ctx: RunContext) -> None:
    for task in schedule.due_tasks(ctx.config, ctx.conn, ctx.clock.now(), ctx.zone):
        schedule.started(ctx.conn, task.name, ctx.clock.now(), ctx.loop.id, task.missed)
        before = set(ctx.collected)
        known = runlog.known_runs(ctx.conn)
        code = ctx.call(task.name, lambda task=task: ctx.modules.run_command(shlex.split(task.command)))
        created = [run for run in runs.find(ctx.conn) if run.id not in known]
        collected = [run.id for run in created if run.stage is RunStage.COLLECT and run.id not in before]
        ctx.collected += collected
        _aggregate_runs(ctx, collected)
        status = RunStatus.OK if code == 0 else RunStatus.FAILED
        if code != 0:
            ctx.anomaly(f"schedule:{task.name}", f"定时任务 {task.command} 退出码 {code}")
        schedule.ended(ctx.conn, task.name, ctx.clock.now(), status)


def _chain_collect_action(ctx: RunContext) -> None:
    assert ctx.request.probe is not None
    _collect(ctx, CollectRequest(ctx.request.probe, level=ctx.request.level))


def _chain_aggregate_action(ctx: RunContext) -> None:
    if ctx.collected:
        _aggregate_runs(ctx, ctx.collected)
    else:
        ctx.call(RunStage.AGGREGATE.value, lambda: ctx.modules.aggregate().run(AggregateRequest()))


class StepNotStarted(RuntimeError):
    """有待处理的对象，模块却无法开始(例如只读 worktree 无法切换)；记为这一步失败，原因写入运行摘要。"""


def _triage_action(ctx: RunContext) -> None:
    chosen = (ctx.request.subject,) if ctx.request.subject else ()
    request = TriageRequest(select=chosen, overrides=ctx.modules.overrides)
    result = ctx.call(RunStage.TRIAGE.value, lambda: ctx.modules.triage().run(request))
    if result.blocked:
        raise StepNotStarted(f"分诊没有开始：{result.blocked}")


def _issue_action(ctx: RunContext) -> None:
    chosen = (ctx.request.subject,) if ctx.request.subject else ()
    ctx.call(RunStage.ISSUE.value, lambda: ctx.modules.issue().create(chosen))


def _track(ctx: RunContext) -> None:
    report = ctx.call(RunStage.RELEASE.value, lambda: ctx.modules.release().track())
    ctx.notes += report.lines


def _staging_action(ctx: RunContext) -> None:
    for issue_id in deployed_issues(ctx):
        result = ctx.call(RunStage.VERIFY.value, lambda issue_id=issue_id: ctx.modules.verify().staging(issue_id))
        ctx.notes.append(f"Issue {issue_id} 部署后确认：{result.message}")
        record = issues.get(ctx.conn, issue_id)
        if record.issue.status is IssueStatus.DONE and record.issue.phase is None:
            drafted = ctx.modules.release().comment(issue_id)
            if drafted.status is HandoffStatus.OK:
                ctx.notes.append(f"Issue {issue_id} 部署后确认通过，PR 回复草稿：{drafted.path}")


def _mirror_action(ctx: RunContext) -> None:
    """对齐 GitHub 镜像；未同步项由运行摘要从数据库列出，整次跳过(公开仓库、读取失败)记为异常。"""
    report = ctx.modules.issue().mirror()
    if report is not None and report.skipped is not None:
        ctx.anomaly("issue-mirror", report.skipped)


def _lessons(ctx: RunContext) -> None:
    ctx.call(RunStage.LEARN.value, lambda: ctx.modules.learn().lessons())


def _weekly_action(ctx: RunContext) -> None:
    due = schedule.weekly_due(ctx.config, ctx.conn, ctx.clock.now(), ctx.zone)
    if due is None:
        return
    schedule.started(ctx.conn, due.name, ctx.clock.now(), ctx.loop.id, due.missed)
    ctx.call(RunStage.LEARN.value, lambda: ctx.modules.learn().report())
    schedule.ended(ctx.conn, due.name, ctx.clock.now(), RunStatus.OK)


def _retention_action(ctx: RunContext) -> None:
    def purge() -> dict[str, int]:
        report = ctx.modules.purge()
        return {"signals": report.signals, "problems": len(report.problems), "runFiles": len(report.run_files),
                "logs": len(report.logs)}

    outcome = idempotency.run_once(ctx.conn, _retention_key(ctx), purge, ctx.clock)
    ctx.notes.append(f"保留期清理：{outcome.result}")


def _health(ctx: RunContext) -> None:
    result = ctx.call(RunStage.LEARN.value, lambda: ctx.modules.learn().health())
    ctx.health = [item for item in result.outputs["health"] if item.get("notify")]
    for item in ctx.health:
        ctx.anomaly(f"health:{item['check']}", item["detail"])


STEPS: tuple[Step, ...] = (
    Step("deployments", Stage.COLLECT, _always, _deployments),
    Step("on-deploy", Stage.COLLECT, _on_deploy, _shallow, models=True),
    Step("commit-patrol", Stage.COLLECT, _commit_patrol, _commit_patrol_action, models=True),
    Step("sources", Stage.COLLECT, _sources, _sources_action),
    Step("scheduled", None, _scheduled, _tasks, models=True),
    Step("chain-collect", "chain", _chain_collect, _chain_collect_action, models=True),
    Step("chain-aggregate", "chain", _chain_aggregate, _chain_aggregate_action),
    Step("triage", Stage.TRIAGE, _triage, _triage_action, models=True),
    Step("issue", Stage.ISSUE, _issue, _issue_action),
    Step("unattended", Stage.FIX, _unattended, _unattended_action, models=True),
    Step("release-track", Stage.RELEASE, _tracked, _track),
    Step("verify-staging", Stage.VERIFY, _staging, _staging_action),
    Step("issue-mirror", Stage.ISSUE, _mirror, _mirror_action),
    Step("learn-lessons", Stage.LEARN, _always, _lessons, models=True),
    Step("weekly", Stage.LEARN, _weekly, _weekly_action, models=True),
    Step("retention", None, _retention, _retention_action),
    Step("health", ALWAYS, _always, _health),
)
EVENT_STEPS = frozenset({"deployments", "on-deploy", "commit-patrol", "sources", "release-track", "verify-staging"})


def selected(step: Step, request: RunRequest) -> bool:
    """链选择下只执行链上的模块步骤与 ALWAYS 步骤；不带链时不执行 chain 步骤；事件运行只执行 EVENT_STEPS。"""
    if request.events:
        return step.name in EVENT_STEPS
    if step.module == ALWAYS:
        return True
    if request.chain is None:
        return step.module != "chain"
    if step.name == "chain-collect":
        return Stage.COLLECT in request.chain
    if step.name == "chain-aggregate":
        return Stage.AGGREGATE in request.chain
    return step.module in (Stage.TRIAGE, Stage.ISSUE) and step.module in request.chain


def preview_steps(ctx: RunContext, steps: Sequence[Step] = STEPS, start: int = 1) -> None:
    """run --dry-run：按当前状态判断各步会不会执行，不执行动作。前面的步骤产生的新问题与 Issue 不计入后面步骤的判断。"""
    for order, step in enumerate(steps, start=start):
        if not selected(step, ctx.request):
            continue
        halted = ctx.halting(step.models)
        reason = None if halted is not None else step.trigger(ctx)
        detail = f"会停下：{halted}" if halted is not None else (reason or "触发条件不满足")
        ctx.outcomes.append(StepOutcome(order, step.name, reason is not None, detail))


def run_steps(ctx: RunContext, steps: Sequence[Step] = STEPS, start: int = 1,
              monotonic: Callable[[], float] = time.monotonic) -> None:
    if ctx.loop is None:
        raise ValueError("执行步骤需要已开始的运行")
    for order, step in enumerate(steps, start=start):
        if not selected(step, ctx.request):
            continue
        halted = ctx.halting(step.models)
        if halted is not None:
            runlog.gate(ctx.loop, step.name, False, halted)
            ctx.outcomes.append(StepOutcome(order, step.name, False, f"已停下：{halted}"))
            continue
        reason = step.trigger(ctx)
        runlog.gate(ctx.loop, step.name, reason is not None, reason or "触发条件不满足")
        if reason is None:
            ctx.outcomes.append(StepOutcome(order, step.name, False, "触发条件不满足"))
            continue
        before = runlog.known_runs(ctx.conn)
        started = monotonic()
        status = RunStatus.OK
        try:
            step.action(ctx)
        except Exception as error:  # noqa: BLE001 每一步是独立的错误边界，失败只记录，后续步骤照常执行
            status = RunStatus.FAILED
            detail = "".join(traceback.format_exception_only(type(error), error)).strip()
            ctx.anomaly(step.name, detail, ctx.layout.relative(ctx.layout.events_log(ctx.clock.now().date())))
        adopted = runlog.adopt(ctx.conn, ctx.loop, before)
        for run in adopted:
            failed = [record for record in handoffs.for_run(ctx.conn, run.id)
                      if record.status is HandoffStatus.FAILED]
            if run.status in FAILED_RUN or failed:
                status = RunStatus.FAILED
                detail = "、".join(f"{record.subject_id}" for record in failed)
                reason = f"运行 {run.id} 为{run.status.label}" + (f"，失败的对象：{detail}" if detail else "")
                ctx.anomaly(step.name, reason, ctx.layout.relative(ctx.layout.run_dir(run.id)))
        duration = round((monotonic() - started) * 1000)
        ctx.outcomes.append(StepOutcome(order, step.name, True, reason, [run.id for run in adopted], status,
                                        duration))
