"""FixService(redesign/05-fix.md)：按「类型 × 档」的流程表分 A 快速、B 标准、C 大任务三条通道，逐步完成第 0 到 9 步。

| 步 | 命令 | 做什么 |
|---|---|---|
| 0 分流 | fix plan | 查流程表决定通道(steps/route.py)，写进度文档；超限时写待决定文档并转人工 |
| 1 准备 | fix prepare 的后续 | 建分支与 worktree、checks.prepare，再在基准版本上全量运行项目检查；不通过即转人工(配置错误) |
| 2 勘察 | fix plan | 只在 B 通道且 Issue 缺根因位置或范围、或类型为安全数据类时 |
| 3 出计划 | fix plan | B、C 由 fix-planner 出计划，程序重评规模档(只升不降)、超出单 PR 上限时要求拆分、标风险；A 由程序按 Issue 生成 |
| 4 确认计划 | fix plan、fix confirm | A 自动；B 按自主决定规则；C 一律交用户，确认后用拆分机制建子 Issue |
| 5 写复现测试 | fix apply | steps/repro_test.py；缺陷类在基准上通过时退回分诊(not-reproduced)，写不出时转人工 |
| 6 写代码 | fix apply | 续接第 5 步同一会话；程序检查档、单 PR 上限、受保护文件、测试与跳过标记；A 超档转 B |
| 7 收集结果 | fix apply | 复现测试、全部项目检查(全量)、相关的其他复现检查、diff 统计，写 result.md |
| 8 评审 | fix apply | A 轻量(微档且检查都通过时跳过)，B 轻量，高风险加深度；阻断项回第 6 步，轮数上限 thresholds.fix.reviewRounds |
| 9 完成 | fix done | 核对改动哈希，复现测试已在 worktree 中随修复提交，Issue 进入合并前验证 |

各步产出交接文档(render/documents.py)，进度写 progress.md(steps/progress.py)。用户的说明与决定(--note、--reject --note、
--accept-design)先写入 decisions.json 再交给各角色。start 进入修复会话(会话只读，只调用 fix 子命令与只读命令)；--here
只做状态转换。--output 模式：plan 不生成待确认操作，apply 不要求计划确认，产出写到输出目录(改动另写 changes.patch)，
不写数据库与 Issue。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import tzinfo
from enum import Enum
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import (
    Access,
    DocumentStatus,
    HandoffStatus,
    IssueEvent,
    IssuePhase,
    IssueStatus,
    Lane,
    OperationKind,
    OperationStatus,
    RegressionResult,
    ReviewCategory,
    RunnerStatus,
    RunStage,
    SizeTier,
    Stage,
    TaskType,
    VerifyPhase,
)
from tightrein.domain.fix import FixRisk
from tightrein.domain.handoff import sections
from tightrein.domain.handoff.document import Reference
from tightrein.domain.sizing import TIER_ORDER
from tightrein.domain.issue import Hold, Issue, IssueContext
from tightrein.domain.issue_sections import CAUSE
from tightrein.guards import diff_rules
from tightrein.guards.policy import GuardSettings
from tightrein.guards.protected import matching_pattern
from tightrein.observability.events import EventLog
from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.orchestrator.policy import autonomy, lanes
from tightrein.orchestrator.policy.lanes import ReproMode
from tightrein.pipeline.checks import output_trim, project_checks
from tightrein.pipeline.checks.regressions import WORKTREE_KINDS, manifest
from tightrein.pipeline.checks.regressions.repo_test_check import RepoTestCheck
from tightrein.pipeline.checks.regressions.runner import RegressionExecutor, RegressionOutcome
from tightrein.pipeline.common import conventions, stage_runs
from tightrein.pipeline.common.stage_runs import StageRun
from tightrein.pipeline.fix.prompts import repro_test as repro_prompt
from tightrein.pipeline.fix.prompts.common import FixCalls, FixPrompt, risk_conditions
from tightrein.pipeline.fix.render import documents, issue_history
from tightrein.pipeline.fix.render import plan as plan_render
from tightrein.pipeline.fix.render import report as report_render
from tightrein.pipeline.fix.steps import (
    checks,
    context,
    decisions,
    execute,
    plan_gate,
    progress,
    repro,
    repro_test,
    review,
    sibling_tests,
    split,
    workspace,
)
from tightrein.pipeline.fix.steps import report as report_step
from tightrein.pipeline.fix.steps.checkpoint import Checkpoint
from tightrein.pipeline.fix.steps import risk as risk_step
from tightrein.pipeline.fix.steps import route as route_step
from tightrein.pipeline.fix.steps import triage_blockers
from tightrein.pipeline.fix.steps.checks import CheckInputs, Finding
from tightrein.pipeline.fix.steps.context import FixContext
from tightrein.pipeline.fix.steps.plan import ProposalSettings, propose
from tightrein.pipeline.fix.steps.progress import BLOCKED, DONE, FAILED, SKIPPED, Tracker
from tightrein.pipeline.fix.steps.route import Route
from tightrein.pipeline.issue.steps import transitions
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.procs import Launcher
from tightrein.retrieval.context import ContextBundle, ContextRequest
from tightrein.runner import stage_yield
from tightrein.runner.result import RESUME_UNSUPPORTED
from tightrein.runner.roles import READ_ONLY_COMMANDS, Overrides
from tightrein.runner.service import Runner
from tightrein.runner.task import Instructions, RunnerTask, Subject
from tightrein.store import idempotency
from tightrein.store.db import transaction
from tightrein.store.files import atomic
from tightrein.store.files import documents as document_files
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.repos import issues, regressions, scores
from tightrein.store.repos.regressions import RegressionCheck
from tightrein.vcs import operations, unattended
from tightrein.vcs.executor import FollowUp
from tightrein.vcs.operations import PendingOperation

STAGE = RunStage.FIX
WORKSPACE_FILE = "workspace.json"
REVIEW_STATE = "review.json"
PATCH_FILE = "changes.patch"
CHECKLIST_PATH = "regressions/{issue}/check.yaml"
SESSION_ROLE = "fix-session"
SESSION_ROUTE = "fix.session"
SESSION_COMMANDS = ("tightrein fix", "tightrein show", *READ_ONLY_COMMANDS)
ACTOR = "fix"
PLAN_APPROVED = "自动确认修复计划：满足"
PLAN_WAITING = "修复计划需要用户确认"
LARGE_WAITING = "大任务(C 通道)的整体方案一律交用户确认"
UNATTENDED_STOPPED = "无人值守修复停下"
BREAKER_HOLD = "熔断"
BASE_CHECKS_FAILED = "项目检查在基准版本上不通过(配置错误)"
OVERSIZE = "超出单个任务的上限"
NO_REPRO = "写不出合格的复现测试"
ENVIRONMENT_BLOCKER = "环境问题"
CHECKPOINT_DIR = "checkpoint"
FAST_CONFIRMED = "A 通道：Issue 即计划，自动确认"
REPRO_TEST_NOTE = "{path} 是本 Issue 的复现测试，由本工具登记并随修复提交；不计入计划外文件，修复不得改动它"
DEFECT_TYPES = frozenset({TaskType.BUG, TaskType.SECURITY, TaskType.DATA, TaskType.FRONTEND, TaskType.DEPENDENCY})
LANE_ORDER = (Lane.FAST, Lane.STANDARD, Lane.LARGE)


class ResumePoint(str, Enum):
    PLAN = "plan"
    CONFIRM = "confirm"
    APPLY = "apply"
    REVIEW = "apply --review-only"  # 通过评审后工作区又有改动(例如合并 main 时改到同一文件)，只需重新评审
    DONE = "done"


@dataclass(frozen=True)
class FixResult:
    issue_id: str
    status: HandoffStatus
    message: str
    handoff: Path | None = None
    operation: str | None = None


@dataclass
class FixDeps:
    layout: WorkspaceLayout
    tool: ToolLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    runner: Runner
    git: Any
    launcher: Launcher
    planner: Any = None
    operations: Any = None
    executor: RegressionExecutor | None = None
    branch_prefix: str | None = None
    context: Callable[[ContextRequest], ContextBundle] | None = None
    endpoints: Callable[[Path], Mapping[str, Any] | None] | None = None
    services: Callable[[Path], Mapping[str, Sequence[str]]] | None = None
    environ: Mapping[str, str] = field(default_factory=dict)
    zone: tzinfo | None = None
    overrides: Overrides = Overrides()


class FixService:
    def __init__(self, deps: FixDeps) -> None:
        self.deps = deps

    # 路径与状态

    @property
    def output_mode(self) -> bool:
        return self.deps.layout.output_dir is not None

    def fix_dir(self, issue_id: str) -> Path:
        if self.output_mode:
            return self.deps.layout.output_path("fixes", issue_id)
        return self.deps.layout.fixes_dir(issue_id)

    def regression_dir(self, issue_id: str) -> Path:
        if self.output_mode:
            return self.deps.layout.output_path("regressions", issue_id)
        return self.deps.layout.regression_dir(issue_id)

    def worktree(self, issue_id: str) -> Path:
        return self.deps.layout.fix_worktree(issue_id)

    def tracker(self, issue_id: str) -> Tracker:
        return Tracker(self.fix_dir(issue_id), issue_id, self.deps.clock, self.deps.config.language, self.deps.zone)

    def writer(self, issue_id: str) -> documents.Writer:
        return documents.Writer(self.fix_dir(issue_id), issue_id, self.deps.config.language, self.deps.zone)

    def _env(self) -> IssueEnv:
        return IssueEnv(self.deps.conn, self.deps.layout, self.deps.clock, self.deps.config, self.deps.zone)

    def _event(self, issue_id: str, event: IssueEvent, issue_context: IssueContext = IssueContext(), *,
               note: str | None = None, updates: Mapping[str, object] | None = None) -> None:
        if self.output_mode:
            return
        record = transitions.record_of(self._env(), issue_id)
        transitions.apply_event(self._env(), record, event, issue_context, actor=ACTOR, note=note, updates=updates)

    def _hold(self, issue_id: str, reason: str, details: str = "") -> None:
        hold = Hold(reason, Stage.FIX, self.deps.clock.now(), details)
        note = f"{reason}：{details}" if details else reason
        self._event(issue_id, IssueEvent.FIX_HELD, IssueContext(hold=hold), note=note)

    def _fixing(self, issue_id: str) -> bool:
        issue = transitions.record_of(self._env(), issue_id).issue
        return issue.status is IssueStatus.IN_PROGRESS and issue.phase is IssuePhase.FIX

    def _workspace(self, issue_id: str) -> dict[str, Any]:
        path = self.deps.layout.fixes_dir(issue_id) / WORKSPACE_FILE
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        head = self.deps.git.head(self.worktree(issue_id))
        return {"branch": head.branch or "detached", "baseCommit": head.commit,
                "worktree": str(self.worktree(issue_id))}

    def _previous(self, issue_id: str) -> tuple[str, dict[str, Any]] | None:
        if self.output_mode:
            path = self.deps.layout.handoff("output", f"fix-{issue_id}")
            if not path.is_file():
                return None
            document = json.loads(path.read_text(encoding="utf-8"))
            return document["status"], document["outputs"]
        return stage_runs.latest_outputs(self.deps.conn, self.deps.layout, STAGE, issue_id)

    def _begin(self) -> StageRun:
        deps = self.deps
        return stage_runs.begin(STAGE, deps.layout, deps.conn, deps.clock, deps.events)

    def _calls(self, run: StageRun, issue_id: str) -> FixCalls:
        deps = self.deps
        return FixCalls(deps.runner, deps.clock, FixPrompt(deps.tool, deps.config, run.id, self.worktree(issue_id)),
                        deps.overrides, None if self.output_mode else deps.conn)

    def _finish(self, run: StageRun, issue_id: str, status: HandoffStatus, outputs: Mapping[str, Any], next_action: str,
                reason: str | None = None, operation: str | None = None) -> FixResult:
        path = run.handoff(STAGE, issue_id, status, outputs, next_action, reason)
        run.end(status)
        return FixResult(issue_id, status, reason or next_action, path, operation)

    def _review_base(self, issue_id: str, base_commit: str) -> str:
        return stage_runs.review_base(self.deps.conn, self.deps.layout, issue_id, base_commit)

    def _base_outputs(self, issue_id: str) -> dict[str, Any]:
        found = self._workspace(issue_id)
        return {"issueId": issue_id, "branch": found["branch"], "worktree": found["worktree"],
                "baseCommit": found["baseCommit"]}

    # 第 1 步 准备

    def prepare(self, issue_id: str) -> FixResult:
        deps = self.deps
        issue = transitions.record_of(self._env(), issue_id).issue
        if issue.status is not IssueStatus.TODO:
            hint = f"先执行 approve {issue_id.lstrip('0') or '0'}" if issue.status is IssueStatus.NEEDS_DECISION else \
                "只为待修的 Issue 建修复分支"
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"Issue {issue_id} 当前为「{issue.status.label}」，{hint}")
        waiting = self.waiting_on(issue)
        if waiting is not None:
            return FixResult(issue_id, HandoffStatus.BLOCKED, waiting)
        if issue.branch is not None and self.worktree(issue_id).is_dir():
            return FixResult(issue_id, HandoffStatus.OK, f"修复分支 {issue.branch} 与 worktree 已存在，直接复用")
        try:
            branch = workspace.branch_name(issue, deps.branch_prefix,
                                           conventions.resolve(deps.config, deps.config.repo), deps.config,
                                           exists=lambda name: deps.git.branch_exists(deps.config.repo, name),
                                           current=issue.branch)
        except workspace.BranchRejected as error:
            return FixResult(issue_id, HandoffStatus.BLOCKED, str(error))
        operation = deps.planner.plan_create_fix_worktree(issue_id, branch)
        if not unattended.release_direct(deps.config, operation.kind):
            return FixResult(issue_id, HandoffStatus.BLOCKED, operations.describe(operation), operation=operation.id)
        result = deps.operations.run_unattended(operation.id, reason=unattended.RELEASE_REASON, clock=deps.clock)
        if result.operation.status is not OperationStatus.EXECUTED:
            return FixResult(issue_id, HandoffStatus.FAILED, f"{operation.id} 建修复分支{result.operation.status.label}："
                             f"{result.error or json.dumps(result.operation.result, ensure_ascii=False)}")
        return FixResult(issue_id, HandoffStatus.OK, f"已直接建立修复分支 {branch} 与 worktree({operation.id})")

    def waiting_on(self, issue: Issue) -> str | None:
        """拆分出的后续子任务在前一个子任务合并之前不能开始(split.waiting_on)。"""
        return split.waiting_on(self.deps.conn, issue)

    def follow_up(self) -> FollowUp:
        return FollowUp(executed=self.on_executed)

    def on_executed(self, operation: PendingOperation) -> None:
        if operation.kind is OperationKind.CREATE_FIX_WORKTREE:
            self._prepared(operation)
        elif operation.kind is OperationKind.FIX_PLAN:
            issue_id = operation.subject_id
            recorded = plan_gate.confirmed(self.fix_dir(issue_id))
            if recorded is not None and recorded["operationId"] == operation.id:
                return
            plan_gate.record(self.fix_dir(issue_id), operation.id, operation.preconditions["planSha256"],
                             self.deps.clock)
            self.tracker(issue_id).mark(4, DONE, f"计划已确认({operation.id})")
            self.writer(issue_id).history(documents.PLAN, self.deps.clock.now(), f"计划已确认({operation.id})",
                                          DocumentStatus.DONE)
            planned = plan_gate.load(self.fix_dir(issue_id))
            route = route_step.load(self.fix_dir(issue_id))
            if planned is not None:
                split.create_follow_ups(self._env(), issue_id, planned, route.task_type if route else None)

    def _prepared(self, operation: PendingOperation) -> None:
        """建好分支与 worktree 后执行 checks.prepare，再在基准版本上全量运行项目检查；任一失败都转人工。"""
        deps = self.deps
        issue_id = operation.subject_id
        worktree = self.worktree(issue_id)
        branch = operation.preconditions["target"]["branch"]
        base = operation.preconditions["base"]
        runs = project_checks.run(workspace.prepare_commands(deps.config), worktree, [], deps.launcher,
                                  deps.layout.fix_file(issue_id, "prepare"),
                                  timeout=project_checks.timeout_seconds(deps.config),
                                  state=lambda root: {}, environ=deps.environ, full=True)
        failed = [run for run in runs if not run.passed]
        text = f"建立修复分支 {branch}，基于 origin/{deps.config.main_branch} 的 {base[:12]}"
        base_checks = "not-run"
        hold = None
        if failed:
            details = "；".join(f"{run.command}(日志 {deps.layout.relative(run.log)})" for run in failed)
            hold = Hold("worktree 准备命令失败", Stage.FIX, deps.clock.now(), details)
            text += f"；准备命令失败，转待决定：{details}"
        else:
            checked = project_checks.run(project_checks.commands(deps.config), worktree, [], deps.launcher,
                                         deps.layout.fix_file(issue_id, "base-checks"),
                                         timeout=project_checks.timeout_seconds(deps.config),
                                         state=lambda root: {}, environ=deps.environ, full=True)
            broken = [run for run in checked if not run.passed]
            base_checks = "failed" if broken else "passed"
            if broken:
                details = "；".join(f"{run.command}(日志 {deps.layout.relative(run.log)})" for run in broken)
                hold = Hold(BASE_CHECKS_FAILED, Stage.FIX, deps.clock.now(),
                            f"{details}；修正 checks.commands、checks.prepare 或项目环境后执行 fix start {issue_id} --force")
                text += f"；{BASE_CHECKS_FAILED}，转待决定：{details}"
        atomic.write_text(deps.layout.fixes_dir(issue_id) / WORKSPACE_FILE, json.dumps(
            {"branch": branch, "baseCommit": base, "worktree": str(worktree), "baseChecks": base_checks},
            ensure_ascii=False, indent=2) + "\n")
        if hold is None:
            transitions.annotate(self._env(), issue_id, text, updates={"branch": branch})
        else:
            self._event(issue_id, IssueEvent.FIX_HELD, IssueContext(hold=hold), note=text, updates={"branch": branch})

    # start 与续接

    def resume_point(self, issue_id: str) -> ResumePoint:
        directory = self.fix_dir(issue_id)
        planned = plan_gate.load(directory)
        route = route_step.load(directory)
        if planned is None or (route is not None and route.lane is not None
                               and planned.get("lane") not in (None, route.lane.value)):
            return ResumePoint.PLAN
        if plan_gate.confirmed(directory) is None:
            return ResumePoint.CONFIRM
        previous = self._previous(issue_id)
        if previous is None or previous[0] != HandoffStatus.OK.value or not previous[1].get("diffHash"):
            return ResumePoint.APPLY
        if self.deps.git.diff_hash(self.worktree(issue_id), self._review_base(
                issue_id, previous[1]["baseCommit"])) != previous[1]["diffHash"]:
            return ResumePoint.REVIEW
        return ResumePoint.DONE

    def start(self, issue_id: str, *, here: bool = False, force: bool = False) -> FixResult:
        deps = self.deps
        issue = transitions.record_of(self._env(), issue_id).issue
        if issue.status is IssueStatus.NEEDS_DECISION and issue.hold is None:
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"先执行 approve {issue_id.lstrip('0') or '0'}")
        if issue.is_closed:
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"Issue {issue_id} 当前为「{issue.status.label}」，不能修复")
        waiting = self.waiting_on(issue)
        if waiting is not None:
            return FixResult(issue_id, HandoffStatus.BLOCKED, waiting)
        if not self.worktree(issue_id).is_dir():
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"先执行 fix prepare {issue_id}")
        if issue.hold is not None and not force:
            return FixResult(issue_id, HandoffStatus.BLOCKED,
                             f"待决定：{issue.hold.reason}({issue.hold.details or '无详情'})；确认继续时加 --force")
        resumed = issue.status is IssueStatus.IN_PROGRESS and issue.phase is IssuePhase.FIX
        if not resumed:
            note = None
            if issue.hold is not None:
                note = f"用户确认继续修复(--force)：{issue.hold.reason}"
            elif issue.status is not IssueStatus.TODO:
                note = f"从「{issue.status.label}」重新进入修复"
            self._event(issue_id, IssueEvent.FIX_STARTED, note=note)
        point = self.resume_point(issue_id)
        if here:
            return FixResult(issue_id, HandoffStatus.OK, f"在当前会话中按 fix skill 继续，下一步：fix {point.value}")
        task = RunnerTask(
            run_id=self._begin().id, stage=Stage.FIX, role=SESSION_ROLE, subject=Subject("issue", issue_id), attempt=1,
            instructions=Instructions(deps.tool.skill("fix").read_text(encoding="utf-8")),
            workdir=self.worktree(issue_id), output_schema=None, access=Access.READ_ONLY,
            allowed_commands=SESSION_COMMANDS, interactive=True, route=SESSION_ROUTE)
        first = self._first_input(issue_id, point)
        result = None
        if resumed:
            result = deps.runner.resume_interactive(task, clock=deps.clock, first_input=first)
        if result is None or result.error_type == RESUME_UNSUPPORTED:
            result = deps.runner.run_interactive(task, clock=deps.clock, first_input=first)
        if not self.output_mode:
            stage_yield.record(deps.conn, task, result, deps.clock.now())
        status = HandoffStatus.OK if result.status is RunnerStatus.OK else HandoffStatus.FAILED
        return FixResult(issue_id, status, f"修复会话结束({result.status.value})")

    def _first_input(self, issue_id: str, point: ResumePoint) -> str:
        deps = self.deps
        record = issues.get(deps.conn, issue_id)
        lines = [f"Issue {issue_id}：{deps.layout.root / record.path}", f"当前停在：fix {point.value}"]
        if record.issue.findings:
            lines.append(f"发现报告：{deps.layout.root / record.issue.findings}")
        verify = stage_runs.latest_outputs(deps.conn, deps.layout, RunStage.VERIFY, issue_id, VerifyPhase.LOCAL)
        if verify is not None:
            lines.append(f"上一次合并前验证：{json.dumps(verify[1], ensure_ascii=False)}")
        for name in (documents.PROGRESS, documents.PLAN):
            path = self.fix_dir(issue_id) / name
            if path.is_file():
                lines.append(f"{'进度' if name == documents.PROGRESS else '修复计划'}：{path}")
        return "\n".join(lines)

    # 第 0 到 4 步

    def plan(self, issue_id: str, note: str | None = None, *, accept_design: bool = False,
             note_source: str = decisions.PLAN_NOTE, plan_gap: str | None = None) -> FixResult:
        """note 与 accept_design 是用户的说明与决定，先持久化；plan_gap 是实施中发现的计划缺口，只交给本次出计划。"""
        deps = self.deps
        if not self.output_mode:
            blocked = self._requires(issue_id, f"先执行 fix start {issue_id}")
            if blocked is not None:
                return blocked
        directory = self.fix_dir(issue_id)
        if accept_design:
            decisions.accept_design(directory, deps.clock)
        elif not self.output_mode and transitions.record_of(self._env(), issue_id).issue.is_manual:
            decisions.accept_design(directory, deps.clock, decisions.MANUAL_DESIGN)
        if note:
            decisions.record(directory, deps.clock, note_source, note)
        decided = decisions.load(directory)
        run = self._begin()
        ctx = context.load(deps.conn, deps.layout, issue_id, deps.context)
        ctx.decisions = decided.render()
        base = self._base_outputs(issue_id)
        route = self._route(issue_id, ctx)
        if route.oversize:
            return self._oversize(run, issue_id, route, base, "分诊预估超出大档的上限")
        self._deterministic_repro(issue_id, ctx)
        if route.lane is Lane.FAST:
            fast = self._fast_plan(ctx, route)
            if fast is not None:
                return self._fast_ready(run, issue_id, ctx, fast, base)
            route = self._upgrade(issue_id, route, route.tier or SizeTier.SMALL, "Issue 没有预估改动文件，需要出计划",
                                  lane=Lane.STANDARD)
        return self._propose(run, issue_id, ctx, route, base, decided, plan_gap)

    def _route(self, issue_id: str, ctx: FixContext) -> Route:
        """第 0 步：已有分流结果(可能已升级)时沿用，否则查流程表；写进度文档。"""
        directory = self.fix_dir(issue_id)
        route = route_step.load(directory)
        if route is None:
            route = route_step.decide(self.deps.config, ctx.issue, ctx.triage)
            route_step.save(directory, route)
        self._track(issue_id, ctx, route)
        tracker = self.tracker(issue_id)
        tracker.mark(0, DONE if not route.oversize else BLOCKED, route.text())
        if tracker.state(1) not in (DONE, None):
            tracker.mark(1, DONE, f"修复 worktree {self.worktree(issue_id)}")
        return route

    def _track(self, issue_id: str, ctx: FixContext, route: Route) -> None:
        config = self.deps.config
        scout = route.lane is Lane.STANDARD and lanes.needs_scout(
            config, route.task_type, has_root_cause=bool(ctx.issue.root_cause), has_scope=bool(self._estimated(ctx)))
        steps = progress.steps_for(route.lane, scout=scout,
                                   repro=lanes.repro_mode(config, route.task_type) is not ReproMode.SKIP)
        self.tracker(issue_id).start(route.lane, steps, f"分流：{route.text()}")

    def _upgrade(self, issue_id: str, route: Route, tier: SizeTier, reason: str, *,
                 lane: Lane | None = None) -> Route:
        upgraded = route_step.upgrade(self.deps.config, route, tier, reason, lane=lane)
        if upgraded != route:
            route_step.save(self.fix_dir(issue_id), upgraded)
            ctx = context.load(self.deps.conn, self.deps.layout, issue_id)
            self._track(issue_id, ctx, upgraded)
            if not self.output_mode:
                transitions.annotate(self._env(), issue_id, f"修复通道调整：{upgraded.changes[-1]}")
        return upgraded

    def _estimated(self, ctx: FixContext) -> list[dict[str, Any]]:
        return list(((ctx.triage.get("estimate") or {}).get("files")) or [])

    def _oversize(self, run: StageRun, issue_id: str, route: Route, base: Mapping[str, Any], why: str) -> FixResult:
        """超限：写待决定文档(建议拆分需求)，Issue 转人工(待决定)。"""
        deps = self.deps
        writer = self.writer(issue_id)
        limit = lanes.tier_limits(deps.config)[SizeTier.LARGE]
        background = (f"Issue {issue_id} 超出单个任务的上限({why})：大档上限为 {limit.max_files} 个文件、"
                      f"{limit.max_lines} 行(不含测试文件)。")
        path = writer.write(documents.DECISION, documents.decision(
            writer, deps.clock.now(), background=background,
            options=[("split", "把需求拆成几个各自不超过大档的 Issue，分别修复", True),
                     ("abandon", "放弃本 Issue(fix abandon)", False)],
            recommendation="拆分需求", reason="超限的改动无法在一次修复中做完并可靠评审"))
        self.tracker(issue_id).mark(0, BLOCKED, OVERSIZE, blocker=f"待决定：{path}")
        self._hold(issue_id, OVERSIZE, f"{why}；拆分建议见 {path}")
        return self._finish(run, issue_id, HandoffStatus.FAILED, base, "由用户拆分需求", f"{OVERSIZE}：{why}")

    def _deterministic_repro(self, issue_id: str, ctx: FixContext) -> None:
        """有确定性来源(api-fuzz、e2e、static)的问题生成复现检查，供合并前验证与部署后确认；用户需求不生成。"""
        deps = self.deps
        if ctx.issue.is_manual or self._repro_checks(issue_id):
            return
        services = deps.services(self.worktree(issue_id)) if deps.services is not None else {}
        generated = repro.generate(deps.conn, deps.layout, deps.config, issue_id, ctx.issue.problems,
                                   ctx.fingerprints, services)
        if generated is not None:
            conn = None if self.output_mode else deps.conn
            repro.write(conn, self.regression_dir(issue_id), CHECKLIST_PATH.format(issue=issue_id), generated)

    def _fast_plan(self, ctx: FixContext, route: Route) -> dict[str, Any] | None:
        """A 通道由程序按 Issue 生成计划：文件取分诊的预估(没有时取根因文件)；没有任何文件时返回 None。"""
        estimated = self._estimated(ctx)
        files = [{"path": item["path"], "isNew": item["isNew"], "reason": None} for item in estimated]
        if not files:
            files = [{"path": path, "isNew": False, "reason": None} for path in ctx.root_files]
        if not files:
            return None
        lines = (ctx.triage.get("estimate") or {}).get("lines") or 0
        direction = (ctx.triage.get("worth") or {}).get("direction") or ctx.issue.title
        acceptance = ctx.acceptance or [ctx.issue.title]
        flag = {"flagged": False}
        all_steps = list(range(1, len(files) + 1))
        cause = (ctx.section(CAUSE) or "").strip() or direction
        hypothesis = {"cause": cause,
                      "evidence": [{"location": location, "fact": cause} for location in ctx.issue.root_cause],
                      "edits": [{"location": location, "change": direction} for location in ctx.issue.root_cause]}
        return {
            "summary": direction, "hypothesis": hypothesis, "steps": [{"file": item["path"], "change": direction, "verification": "复现测试与项目检查"}
                                            for item in files],
            "files": files, "estimate": {"files": len(files), "lines": lines}, "split": None, "protectedTouches": [],
            "flags": {"design": flag, "dataStructure": flag, "publicContract": flag}, "migration": None,
            "newDependencies": [], "deletions": [],
            "acceptanceMapping": [{"criterion": item, "steps": all_steps} for item in acceptance],
            "userVisibleChange": ctx.issue.title, "affectedEndpoints": [], "affectedPages": [], "notDoing": [],
            "userDecisions": [], "lane": Lane.FAST.value, "tier": (route.tier or SizeTier.SMALL).value,
        }

    def _fast_ready(self, run: StageRun, issue_id: str, ctx: FixContext, planned: dict[str, Any],
                    base: Mapping[str, Any]) -> FixResult:
        """A 通道：保存程序生成的计划并自动确认(不生成待确认操作)。"""
        deps = self.deps
        directory = self.fix_dir(issue_id)
        risk = risk_step.plan_risk(ctx, None, deps.config, self._endpoint_files(issue_id))
        planned = {**planned, "risk": risk.to_dict()}
        path = self._save_plan(issue_id, planned, DocumentStatus.DONE)
        plan_gate.record(directory, None, plan_gate.sha256(path), deps.clock, note=FAST_CONFIRMED)
        outputs = self._plan_outputs(base, planned, path, None)
        return self._finish(run, issue_id, HandoffStatus.OK, outputs, f"执行 fix apply {issue_id}")

    def _endpoint_files(self, issue_id: str) -> set[str]:
        found = self.deps.endpoints(self.worktree(issue_id)) if self.deps.endpoints is not None else None
        return risk_step.endpoint_files(found)

    def _save_plan(self, issue_id: str, planned: Mapping[str, Any], status: DocumentStatus) -> Path:
        writer = self.writer(issue_id)
        document = documents.plan(writer, self.deps.clock.now(), planned, status=status,
                                  attention=plan_render.attention(planned))
        text = document_files.render(document, self.deps.config.language, self.deps.zone)
        return plan_gate.save(self.fix_dir(issue_id), planned, text)

    def _plan_outputs(self, base: Mapping[str, Any], planned: Mapping[str, Any], path: Path,
                      frontend: Mapping[str, Any] | None) -> dict[str, Any]:
        return {**base, "plan": {"path": self.deps.layout.relative(path) if not self.output_mode else str(path),
                                 "sha256": plan_gate.sha256(path), "operationId": None, "confirmedAt": None},
                "risk": {"plan": planned.get("risk"), "apply": None},
                "affectedEndpoints": planned["affectedEndpoints"], "affectedPages": planned["affectedPages"],
                "migration": planned["migration"], "protectedTouches": planned["protectedTouches"],
                "split": planned.get("split"), "frontendDesign": frontend}

    def _propose(self, run: StageRun, issue_id: str, ctx: FixContext, route: Route, base: Mapping[str, Any],
                 decided: decisions.Decisions, plan_gap: str | None) -> FixResult:
        """B、C 通道：需要时勘察，fix-planner 出计划，程序重评规模档并确认计划。"""
        deps = self.deps
        tracker = self.tracker(issue_id)
        previous = self._previous(issue_id)
        review_notes = tuple(failure["problem"] for item in (previous[1].get("rounds") or [])[-1:]
                             for failure in item["failures"]) if previous is not None else ()
        if plan_gap:
            review_notes = (*review_notes, plan_gap)
        guard = GuardSettings.from_config(deps.config)
        scout = route.lane is Lane.STANDARD and lanes.needs_scout(
            deps.config, route.task_type, has_root_cause=bool(ctx.issue.root_cause),
            has_scope=bool(self._estimated(ctx)))
        settings = ProposalSettings(
            worktree=self.worktree(issue_id), config=deps.config, protected=tuple(guard.protected_paths),
            max_files=guard.max_files, max_lines=guard.max_lines, rounds=deps.config.whole_threshold("fix.planRounds"),
            endpoints=self._endpoint_files(issue_id), review_notes=review_notes,
            design_accepted=decided.design_accepted, scout=scout, large=route.lane is Lane.LARGE,
            test_paths=tuple(guard.test_paths))
        proposal = propose(self._calls(run, issue_id), ctx, settings)
        if proposal.scouting is not None:
            writer = self.writer(issue_id)
            path = writer.write(documents.SCOUT, documents.scout(writer, deps.clock.now(), proposal.scouting))
            tracker.mark(2, DONE, f"勘察结果 {path.name}")
            if not self.output_mode:
                transitions.annotate(self._env(), issue_id, f"勘察结果：{deps.layout.relative(path)}")
        if proposal.design is not None:
            reason = f"根因在设计本身：{proposal.design['rootCause']}；局部修补不彻底：{proposal.design['reason']}"
            tracker.mark(3, BLOCKED, "设计问题", blocker=reason)
            self._hold(issue_id, "设计问题", reason)
            return self._finish(run, issue_id, HandoffStatus.FAILED, base, "由用户决定找代码作者讨论或扩大修复范围",
                                reason)
        if proposal.plan is None:
            reason = f"计划重出 {settings.rounds} 次仍未通过：" + "；".join(proposal.problems)
            tracker.mark(3, FAILED, "计划重出超过上限", blocker=reason)
            self._hold(issue_id, "修复计划重出超过上限", reason)
            return self._finish(run, issue_id, HandoffStatus.FAILED, base, "转人工", reason)
        planned = proposal.plan
        estimate = planned["estimate"]
        tier = lanes.tier_of(deps.config, estimate["files"], estimate["lines"])
        later = split.follow_ups(planned)
        total = tier
        if later:
            total = lanes.tier_of(deps.config, estimate["files"] + sum(item["estimate"]["files"] for item in later),
                                  estimate["lines"] + sum(item["estimate"]["lines"] for item in later))
        route = self._upgrade(issue_id, route, total, "按计划的预估改动重评规模档")
        if route.oversize:
            return self._oversize(run, issue_id, route, base, f"计划预估合计 {total.label}档")
        planned = {**planned, "lane": route.lane.value, "tier": tier.value}
        return self._plan_ready(run, issue_id, ctx, planned, base, route, proposal.frontend)

    def _requires(self, issue_id: str, hint: str) -> FixResult | None:
        """修复的各步要求 Issue 进行中且处于修复阶段，且修复 worktree 存在。待决定的 Issue 提示带上 --force。"""
        if not self._fixing(issue_id):
            if transitions.record_of(self._env(), issue_id).issue.hold is not None:
                hint = f"{hint} --force"
            return FixResult(issue_id, HandoffStatus.BLOCKED, hint)
        if not self.worktree(issue_id).is_dir():
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"修复 worktree 不存在，先执行 fix prepare {issue_id}")
        return None

    def _repro_checks(self, issue_id: str) -> list[RegressionCheck]:
        if not self.output_mode:
            return repro.existing(self.deps.conn, issue_id)
        directory = self.regression_dir(issue_id)
        if not (directory / manifest.CHECKLIST).is_file():
            return []
        return [RegressionCheck(issue_id, entry.id, entry.kind, CHECKLIST_PATH.format(issue=issue_id),
                                manifest.entry_hash(directory, entry)) for entry in manifest.load(directory).checks]

    def _plan_ready(self, run: StageRun, issue_id: str, ctx: FixContext, planned: dict[str, Any],
                    base: Mapping[str, Any], route: Route, frontend: Mapping[str, Any] | None = None) -> FixResult:
        deps = self.deps
        tracker = self.tracker(issue_id)
        path = self._save_plan(issue_id, planned, DocumentStatus.PENDING)
        tracker.mark(3, DONE, f"计划 {path.name}(通道 {route.text()})")
        outputs = self._plan_outputs(base, planned, path, frontend)
        if self.output_mode:
            return self._finish(run, issue_id, HandoffStatus.OK, outputs, "执行 fix apply")
        notes = plan_render.attention(planned)
        description = f"确认 Issue {issue_id} 的修复计划(计划：{path}；通道 {route.text()})\n" + (
            "需要特别关注：\n" + "\n".join(f"- {item}" for item in notes) if notes else "没有需要特别关注的事项")
        if frontend is not None and frontend["error"] is not None:
            description += f"\n前端设计说明没有产出，写代码时只按计划实施：{frontend['error']}"
        operation = plan_gate.request(deps.conn, deps.clock, repo=str(deps.config.repo), issue_id=issue_id,
                                      plan_sha=plan_gate.sha256(path), text=description)
        outputs["plan"]["operationId"] = operation.id
        tracker.mark(4, BLOCKED, f"等待确认 {operation.id}", blocker=f"fix confirm {issue_id}")
        result = self._finish(run, issue_id, HandoffStatus.BLOCKED, outputs,
                              f"用户确认计划：fix confirm {issue_id}", f"等待确认修复计划 {operation.id}",
                              operation.id)
        decided = self.auto_confirm(issue_id)
        if decided is None:
            return result
        if decided.status is HandoffStatus.OK:
            return replace(result, status=HandoffStatus.OK, message=decided.message)
        return replace(result, message=f"{result.message}；{decided.message}")

    # confirm

    def confirm(self, issue_id: str, *, reject: bool = False, note: str | None = None) -> FixResult:
        deps = self.deps
        state = self.fix_dir(issue_id) / REVIEW_STATE
        if state.is_file():
            if reject:
                state.unlink()
                return self.plan(issue_id, note, note_source=decisions.REJECT_NOTE)
            if note:
                return self._accept_review(issue_id, note)
        pending = plan_gate.pending(deps.conn, issue_id)
        if not pending:
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"没有待确认的计划，先执行 fix plan {issue_id}")
        operation = pending[-1]
        if reject:
            deps.operations.reject(operation.id, note=note, clock=deps.clock)
            if note:
                return self.plan(issue_id, note, note_source=decisions.REJECT_NOTE)
            return FixResult(issue_id, HandoffStatus.BLOCKED, "计划已拒绝；带 --note 说明要求即可重出计划")
        deps.operations.confirm(operation.id, confirmed_by="user", clock=deps.clock)
        result = deps.operations.execute(operation.id, clock=deps.clock)
        self.on_executed(result.operation)
        return FixResult(issue_id, HandoffStatus.OK, f"已确认计划({operation.id})，下一步 fix apply {issue_id}",
                         operation=operation.id)

    def auto_confirm(self, issue_id: str) -> FixResult | None:
        """关卡 gates.plan-confirm 为 auto 且有待确认的计划时按规则判断(orchestrator/policy/autonomy.py)：满足即自动确认(等同
        fix confirm)，否则停下交用户，同一份计划的原因只记一次；大任务的整体方案一律交用户。没有可判断的计划时返回 None。"""
        deps = self.deps
        if self.output_mode or not gates.auto(deps.config, Gate.PLAN_CONFIRM):
            return None
        pending = plan_gate.pending(deps.conn, issue_id)
        planned = plan_gate.load(self.fix_dir(issue_id))
        if not pending or planned is None:
            return None
        operation = pending[-1]
        if planned.get("lane") == Lane.LARGE.value:
            decision = autonomy.Decision(False, (LARGE_WAITING,))
        else:
            previous = self._previous(issue_id)
            frontend = previous[1].get("frontendDesign") if previous is not None else None
            decision = autonomy.plan_confirmation(deps.config, planned, frontend)
        text = decision.text(PLAN_APPROVED, PLAN_WAITING)
        if not decision.approved:
            key = f"autonomy:{operation.idempotency_key}"
            if idempotency.get(deps.conn, key) is None:
                idempotency.begin(deps.conn, key, deps.clock)
                idempotency.complete(deps.conn, key, decision.to_dict(), deps.clock)
                transitions.annotate(self._env(), issue_id, text)
            return FixResult(issue_id, HandoffStatus.BLOCKED, text, operation=operation.id)
        transitions.annotate(self._env(), issue_id, text)
        result = deps.operations.run_unattended(operation.id, reason=text, clock=deps.clock)
        self.on_executed(result.operation)
        return FixResult(issue_id, HandoffStatus.OK, f"{text}；已确认计划({operation.id})，下一步 fix apply {issue_id}",
                         operation=operation.id)

    # 第 5 到 8 步

    def apply(self, issue_id: str, *, review_only: bool = False) -> FixResult:
        deps = self.deps
        directory = self.fix_dir(issue_id)
        planned = plan_gate.load(directory)
        confirmation = plan_gate.confirmed(directory)
        if not self.output_mode:
            blocked = self._requires(issue_id, f"先执行 fix start {issue_id}")
            if blocked is not None:
                return blocked
            if planned is None or confirmation is None:
                return FixResult(issue_id, HandoffStatus.BLOCKED, "计划已变化或尚未确认，请重新确认")
        elif planned is None:
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"先执行 fix plan {issue_id}")
        if self.resume_point(issue_id) is ResumePoint.PLAN:
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"修复通道已调整，先执行 fix plan {issue_id}")
        worktree = self.worktree(issue_id)
        previous = self._previous(issue_id)
        if (not review_only and previous is not None and previous[0] == HandoffStatus.OK.value
                and previous[1].get("diffHash") == deps.git.diff_hash(worktree, self._review_base(issue_id,
                                                                                         previous[1]["baseCommit"]))):
            return FixResult(issue_id, HandoffStatus.OK, "改动与上次通过评审时相同，直接返回上次结果")
        run = self._begin()
        ctx = context.load(deps.conn, deps.layout, issue_id, deps.context)
        ctx.decisions = decisions.load(directory).render()
        route = route_step.load(directory) or route_step.decide(deps.config, ctx.issue, ctx.triage)
        return _Apply(self, run, ctx, planned, confirmation, route).run(review_only)

    def _accept_review(self, issue_id: str, note: str) -> FixResult:
        state = self.fix_dir(issue_id) / REVIEW_STATE
        saved = json.loads(state.read_text(encoding="utf-8"))
        state.unlink()
        run = self._begin()
        outputs = saved["outputs"]
        outputs["unverified"] = [*outputs.get("unverified", []), {"item": "评审给出「无法判断」的项",
                                                                 "reason": f"用户判断：{note}"}]
        return self._report(run, issue_id, outputs, saved["reviewItems"], saved["patch"], saved["round"])

    def _report(self, run: StageRun, issue_id: str, outputs: dict[str, Any], review_items: Sequence[Mapping[str, Any]],
                patch: str, round_number: int) -> FixResult:
        deps = self.deps
        worktree = self.worktree(issue_id)
        outputs["diffHash"] = deps.git.diff_hash(worktree, self._review_base(issue_id, outputs["baseCommit"]))
        path = run.handoff(STAGE, issue_id, HandoffStatus.OK, outputs, f"执行 fix done {issue_id}，进入合并前验证")
        document = json.loads(path.read_text(encoding="utf-8"))
        report_render.write(self.fix_dir(issue_id) / "report.md", document)
        tests = manifest.placed(self.regression_dir(issue_id), worktree)
        results = report_step.score(document, report_step.without_files(patch, tests),
                                    GuardSettings.from_config(deps.config), review_items, deps.clock)
        if self.output_mode:
            atomic.write_text(deps.layout.output_path(PATCH_FILE), patch)
        else:
            with transaction(deps.conn):
                for record in report_step.score_records(results, run.id, issue_id, round_number, deps.clock):
                    scores.append(deps.conn, record)
            transitions.annotate(self._env(), issue_id, issue_history.fix_line(outputs))
        tracker = self.tracker(issue_id)
        if tracker.state(8) is not None and tracker.state(8) != SKIPPED:
            tracker.mark(8, DONE, "评审通过")
        run.end(HandoffStatus.OK)
        return FixResult(issue_id, HandoffStatus.OK, "评审通过，改动留在修复 worktree 中、未提交", path)

    # 第 9 步

    def done(self, issue_id: str) -> FixResult:
        deps = self.deps
        blocked = self._requires(issue_id, f"先执行 fix start {issue_id}")
        if blocked is not None:
            return blocked
        previous = self._previous(issue_id)
        if previous is None or previous[0] != HandoffStatus.OK.value or not previous[1].get("diffHash"):
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"最近一次 fix apply 没有通过，先执行 fix apply {issue_id}")
        outputs = previous[1]
        if deps.git.diff_hash(self.worktree(issue_id), self._review_base(issue_id, outputs["baseCommit"])) \
                != outputs["diffHash"]:
            return FixResult(issue_id, HandoffStatus.BLOCKED,
                             f"apply 之后工作区有新改动，先执行 fix apply {issue_id} --review-only")
        tests = sorted(manifest.placed(self.regression_dir(issue_id), self.worktree(issue_id)))
        text = f"改动 {len(outputs.get('changedFiles') or [])} 个文件" + (
            f"，复现测试 {'、'.join(tests)} 随修复提交" if tests else "") + "，进入合并前验证"
        self._event(issue_id, IssueEvent.FIX_DONE, note=text)
        self.tracker(issue_id).mark(9, DONE, text)
        return FixResult(issue_id, HandoffStatus.OK, f"已进入合并前验证，下一步 verify local {issue_id}")

    def stop_unattended(self, issue_id: str, reason: str) -> None:
        """无人值守修复停下(gates.fix-session 为 auto)：待修或修复阶段的 Issue 转待决定(已因修复中的原因转待决定的不改)，
        GitHub 镜像评论写明原因。"""
        issue = transitions.record_of(self._env(), issue_id).issue
        held = issue.status is IssueStatus.NEEDS_DECISION and issue.hold is not None
        if not (held or issue.status is IssueStatus.TODO or self._fixing(issue_id)):
            return
        text = f"{UNATTENDED_STOPPED}：{reason}"
        if not held:
            self._hold(issue_id, UNATTENDED_STOPPED, reason)

    def hold(self, issue_id: str, reason: str) -> None:
        """编排的熔断(orchestrator/breaker.py)：待修与进行中的 Issue 转待决定。"""
        issue = transitions.record_of(self._env(), issue_id).issue
        if issue.status not in (IssueStatus.TODO, IssueStatus.IN_PROGRESS):
            return
        self._hold(issue_id, BREAKER_HOLD, reason)

    def abandon(self, issue_id: str, reason: str) -> FixResult:
        if not self._fixing(issue_id):
            return FixResult(issue_id, HandoffStatus.BLOCKED, f"Issue {issue_id} 不在修复中")
        self._hold(issue_id, "用户停止修复", reason)
        return FixResult(issue_id, HandoffStatus.OK, "已停止修复并转待决定，改动保留在 worktree 中")


class _Apply:
    """一次 fix apply：第 5 步写复现测试(还没有合格的测试时)，再循环第 6 步写代码、第 7 步收集结果、第 8 步评审，
    每一轮写入 rounds/<轮次>/；轮数上限 thresholds.fix.reviewRounds。改动量超出单 PR 上限时交回收敛一次，仍超出时转人工；
    A 通道超出当前档时转 B(保留复现测试)。"""

    def __init__(self, service: FixService, run: StageRun, ctx: FixContext, planned: dict[str, Any],
                 confirmation: Mapping[str, Any] | None, route: Route) -> None:
        self.service = service
        self.deps = service.deps
        self.run_ = run
        self.ctx = ctx
        self.plan = planned
        self.confirmation = confirmation
        self.route = route
        self.lane = Lane.STANDARD if route.lane is Lane.LARGE else (route.lane or Lane.STANDARD)
        self.issue_id = ctx.issue_id
        self.worktree = service.worktree(self.issue_id)
        self.base = service._base_outputs(self.issue_id)
        self.review_base = service._review_base(self.issue_id, self.base["baseCommit"])
        self.calls = service._calls(run, self.issue_id)
        self.settings = GuardSettings.from_config(self.deps.config)
        self.tracker = service.tracker(self.issue_id)
        self.writer = service.writer(self.issue_id)
        self.rounds: list[dict[str, Any]] = []
        self.discarded: list[dict[str, Any]] = []
        self.execution: execute.Execution | None = None
        self.project_runs: list[project_checks.CheckRun] = []
        self.changes: list[diff_rules.ChangedFile] = []
        self.own: list[RegressionOutcome] = []
        self.repro_tests: set[str] = set()  # 内容与登记副本一致、放在 worktree 中的本 Issue 复现测试
        self.size_corrected = False  # 本次实施已因改动量超出上限交回 fix-executor 收敛过一次
        self.session_id: str | None = None
        self.task_text = ""
        self.repro_text = ""

    def _round_number(self) -> int:
        directory = self.service.fix_dir(self.issue_id) / "rounds"
        existing = [int(item.name) for item in directory.iterdir() if item.name.isdigit()] if directory.is_dir() else []
        return max(existing, default=0) + 1

    def run(self, review_only: bool) -> FixResult:
        self.task_text = self._task_document()
        stopped = self._repro_step()
        if stopped is not None:
            return stopped
        return self._loop(review_only)

    # 任务文档

    def _task_document(self) -> str:
        deps = self.deps
        ctx = self.ctx
        tier = self.route.tier or SizeTier.SMALL
        limit = lanes.tier_limits(deps.config).get(tier)
        constraints = [f"单个 PR 改动不超过 {self.settings.max_files} 个文件、{self.settings.max_lines} 行(不含测试文件)",
                       "不改受保护文件(计划确认时列出的除外)，不删改已有测试，不加跳过标记，不针对测试数据写特殊处理"]
        if limit is not None:
            constraints.insert(0, f"当前规模档 {tier.label}：不超过 {limit.max_files} 个文件、{limit.max_lines} 行")
        acceptance = [*ctx.acceptance, "复现测试修复前失败、修复后通过", "现有测试与项目检查全部通过"]
        references = [Reference(deps.layout.relative(deps.layout.root / ctx.record.path), "Issue")]
        for name, note in ((documents.PLAN, "计划"), (documents.SCOUT, "勘察结果")):
            path = self.service.fix_dir(self.issue_id) / name
            if path.is_file():
                references.append(Reference(str(path), note))
        document = documents.task(
            self.writer, deps.clock.now(), goal=f"Issue {self.issue_id}：{ctx.issue.title}\n\n{self.plan['summary']}",
            inputs=[f"Issue 文件 {ctx.record.path}", "已确认的计划(见下文)", f"通道 {self.route.text()}"],
            constraints=constraints, acceptance=acceptance,
            deliverables="第一轮：一个复现测试与运行它的命令；第二轮：代码改动，结构化结果写明改了什么、偏离与遗留。",
            references=references)
        self.writer.write(documents.TASK, document)
        return sections.strip_frontmatter(document_files.render(document, deps.config.language, deps.zone)).strip()

    # 第 5 步

    def _repro_step(self) -> FixResult | None:
        deps = self.deps
        service = self.service
        mode = lanes.repro_mode(deps.config, self.route.task_type)
        fix_dir = service.fix_dir(self.issue_id)
        saved = repro_test.load(fix_dir)
        if mode is ReproMode.SKIP:
            reason = f"类型为{self.route.task_type.label}，不写复现测试"
            repro_test.save(fix_dir, {"outcome": SKIPPED, "asExpected": True, "baseCommit": self.base["baseCommit"],
                                      "reason": reason})
            if self.tracker.state(5) is not None:
                self.tracker.mark(5, SKIPPED, reason)
            return None
        if saved is not None and saved.get("asExpected"):
            self.repro_text = self._repro_summary(saved)
            self.session_id = saved.get("sessionId") if mode is ReproMode.SAME_SESSION else None
            return None
        expects_pass = lanes.expects_pass(deps.config, self.route.task_type)
        role = repro_prompt.WRITER if mode is ReproMode.INDEPENDENT else repro_prompt.EXECUTOR
        commands = tuple(project_checks.commands(deps.config))
        rules = repro_test.TestRules(
            self.worktree, self.settings.test_paths, commands, lambda root: deps.git.status(root).changed_paths,
            deps.git.untracked,
            RepoTestCheck.configured(deps.launcher, deps.environ, deps.config,
                                     lambda command, file: project_checks.repro_test_cwd(commands, command, file)),
            service.regression_dir(self.issue_id), deps.layout.raw_dir(self.run_.id))

        siblings = sibling_tests.read(self.worktree, sibling_tests.find(
            deps.git.files(self.worktree), self.ctx.root_files, self.settings.test_paths,
            deps.config.get("fix.repro.testFilePatterns"), int(deps.config.get("fix.repro.siblingTests"))),
            int(deps.config.get("fix.repro.siblingLines")))

        # 与写代码同一会话时条件须与写代码一轮相同，工具与模型才一致
        conditions = risk_conditions(self.plan) if role == repro_prompt.EXECUTOR else ()

        def build(attempt: int, feedback: Sequence[str]) -> RunnerTask:
            return repro_prompt.task(self.calls.prompt, self.ctx, self.task_text, attempt, role=role,
                                     expects_pass=expects_pass, test_paths=self.settings.test_paths,
                                     prefixes=project_checks.repro_test_prefixes(commands),
                                     check_commands=execute.allowed_commands(commands), siblings=siblings,
                                     feedback=feedback, conditions=conditions)

        found = repro_test.write(self.calls, build, rules, expects_pass=expects_pass,
                                 defect=self.route.task_type in DEFECT_TYPES and not self.ctx.issue.is_manual,
                                 rounds=deps.config.whole_threshold("fix.planRounds"))
        written = found.output or {}
        values = {"role": role, "file": written.get("file"), "command": written.get("command"),
                  "location": written.get("location"), "expected": "pass" if expects_pass else "fail",
                  "baseCommit": self.base["baseCommit"], "baseResult": found.base_result, "outcome": found.outcome,
                  "asExpected": found.passed, "sessionId": found.session_id, "reason": found.reason}
        repro_test.save(fix_dir, values)
        if found.passed:
            conn = None if service.output_mode else deps.conn
            check = repro_test.register(conn, service.regression_dir(self.issue_id),
                                        CHECKLIST_PATH.format(issue=self.issue_id), self.issue_id,
                                        self.ctx.fingerprints, found, self.worktree)
            values["checkId"] = check.check_id
            repro_test.save(fix_dir, values)
            self.tracker.mark(5, DONE, f"复现测试 {values['file']}，基准版本上{'通过' if expects_pass else '失败'}")
            self.repro_text = self._repro_summary(values)
            self.session_id = found.session_id if mode is ReproMode.SAME_SESSION else None
            return None
        if found.outcome == repro_test.NOT_REPRODUCED:
            self.tracker.mark(5, FAILED, found.reason, blocker="退回分诊")
            self.service._event(self.issue_id, IssueEvent.NOT_REPRODUCED, note=found.reason)
            return self._stop(HandoffStatus.FAILED, f"{found.reason}；已退回分诊")
        if found.outcome == repro_test.ENVIRONMENT:
            self.tracker.mark(5, BLOCKED, found.reason, blocker=ENVIRONMENT_BLOCKER)
            return self._stop(HandoffStatus.BLOCKED, found.reason)
        self.tracker.mark(5, BLOCKED, found.reason, blocker=NO_REPRO)
        return self._held(NO_REPRO, f"{found.reason}；补充复现线索后执行 fix start {self.issue_id} --force 与 "
                                    f"fix apply {self.issue_id}，或 fix abandon {self.issue_id}")

    @staticmethod
    def _repro_summary(values: Mapping[str, Any]) -> str:
        expected = "通过" if values.get("expected") == "pass" else "失败"
        return (f"- 测试文件：`{values['file']}`\n- 命令：`{values['command']}`\n- 被测位置：`{values['location']}`\n"
                f"- 基准版本上按预期{expected}：{values['baseResult']}")

    # 第 6 到 8 步

    def _loop(self, review_only: bool) -> FixResult:
        deps = self.deps
        limit = deps.config.whole_threshold("fix.reviewRounds")
        commands = project_checks.commands(deps.config)
        corrections: list[str] = []
        skip_execute = review_only
        checkpoint = Checkpoint(deps.git, self.worktree, self.review_base,
                                self.service.fix_dir(self.issue_id) / CHECKPOINT_DIR)
        rollback_at = deps.config.whole_threshold("fix.checkpointRollbackFindings")
        if not review_only:
            checkpoint.clear()  # 新的一次实施不沿用此前实施的检查点；评审后续接时保留
        for correction in range(limit + 1):
            number = self._round_number()
            if not skip_execute:
                self.execution = execute.execute(
                    self.calls, self.ctx, self.plan, number, commands=commands,
                    approved_protected=[item["path"] for item in self.plan["protectedTouches"]],
                    budget=context.budget(deps.config, self.ctx.complexity), task_text=self.task_text,
                    repro_test=self.repro_text, corrections=corrections, session_id=self.session_id)
                self.session_id = self.execution.session_id
                stopped = self._execution_findings()
                if stopped is not None:
                    findings, category = stopped
                    self._record(number, checks.CheckReport(tuple(findings), (), ()), None, [], findings)
                    outcome = self._handle(findings, category, correction == limit, "")
                    if outcome is not None:
                        return outcome
                    corrections = [item.text() for item in findings]
                    continue
            skip_execute = False
            report, changes, patch, others = self._checks(number, commands)
            actual = self._actual_tier(changes)
            upgraded = self._over_tier(actual)
            if upgraded is not None:
                return upgraded
            results = self._results(report, changes, others)
            rolled_back = self._checkpoint(checkpoint, number, report, rollback_at, last=correction == limit)
            self.tracker.mark(6, DONE, f"第 {number} 轮改动 {len(changes)} 个文件，规模档 {actual.label}")
            if report.not_run:
                self._record(number, report, None, [], [])
                self.tracker.mark(7, BLOCKED, "；".join(report.not_run), blocker="检查未运行")
                return self._stop(HandoffStatus.BLOCKED, "；".join(report.not_run))
            if report.findings:
                groups = triage_blockers.classify(report.findings)
                category = triage_blockers.first(groups)
                self._record(number, report, None, [], report.findings)
                self.tracker.mark(7, FAILED, f"第 {number} 轮有 {len(report.findings)} 项不通过，交回修改")
                oversize = [item for item in report.findings if item.check == checks.SIZE]
                if oversize and self.size_corrected:
                    return self._held("改动量仍超出上限", "；".join(item.text() for item in oversize)
                                      + f"；用 fix plan {self.issue_id} --note <拆分或缩小范围的要求> 重出计划，"
                                      f"或 fix abandon {self.issue_id}")
                outcome = self._handle(report.findings, category, correction == limit, patch)
                if outcome is not None:
                    return outcome
                corrections = [item.text() for item in groups[category]]
                if rolled_back:
                    corrections.insert(0, rolled_back)
                self.size_corrected = self.size_corrected or any(item.check == checks.SIZE
                                                                 for item in groups[category])
                continue
            self.tracker.mark(7, DONE, "复现测试与全部检查通过")
            risk = risk_step.apply_risk(changes, self.plan, self.ctx, deps.config, self.service._endpoint_files(
                self.issue_id))
            modes = lanes.review_modes(deps.config, self.lane, actual_tier=actual, checks_passed=True,
                                       high_risk=risk.high)
            if not modes:
                if self.tracker.state(8) is not None:
                    self.tracker.mark(8, SKIPPED, "A 通道微档且检查都通过，跳过评审")
                self._record(number, report, risk, [], [])
                return self.service._report(self.run_, self.issue_id, self._outputs(risk, others), [], patch, number)
            notes = [f"疑似写死：{item.path}：{item.detail}" for item in report.hardcode]
            notes += [REPRO_TEST_NOTE.format(path=path) for path in sorted(self.repro_tests)]
            reviewed: list[review.Review] = []
            for mode in modes:
                found = review.review(self.calls, self.ctx, self.plan, patch, notes, mode, results, number)
                reviewed.append(found)
                self._review_document(number, found)
                if not found.passed:
                    break
            for found in reviewed:
                self.discarded += found.discarded
            findings = [finding for found in reviewed for finding in found.findings()]
            self._record(number, report, risk, reviewed, findings)
            last = reviewed[-1]
            if last.status is not RunnerStatus.OK:
                if correction == limit:
                    return self._held("评审没有给出结果", f"fix-reviewer 返回 {last.status.value}")
                skip_execute = True
                continue
            if last.unknown:
                return self._await_review(number, risk, last, patch, others)
            if findings:
                self.tracker.mark(8, FAILED, f"第 {number} 轮评审有 {len(findings)} 个阻断项，交回修改")
                groups = triage_blockers.classify(findings)
                category = triage_blockers.first(groups)
                outcome = self._handle(findings, category, correction == limit, patch)
                if outcome is not None:
                    return outcome
                corrections = [item.text() for item in groups[category]]
                continue
            items = [item for found in reviewed for item in (found.output or {}).get("items", [])]
            return self.service._report(self.run_, self.issue_id, self._outputs(risk, others), items, patch, number)
        return self._held("修正与复评超过上限", f"已修正 {limit} 轮仍未通过")

    def _counted(self, changes: Sequence[diff_rules.ChangedFile]) -> list[diff_rules.ChangedFile]:
        return [item for item in changes if matching_pattern(item.path, self.settings.test_paths) is None]

    def _actual_tier(self, changes: Sequence[diff_rules.ChangedFile]) -> SizeTier:
        counted = self._counted(changes)
        return lanes.tier_of(self.deps.config, len(counted),
                             sum(item.lines_added + item.lines_removed for item in counted))

    def _over_tier(self, actual: SizeTier) -> FixResult | None:
        """A 通道的实际改动超出当前档时转 B(已写的复现测试保留，下一步 fix plan)；B 通道只记录升档。"""
        current = self.route.tier or SizeTier.SMALL
        if TIER_ORDER.index(actual) <= TIER_ORDER.index(current):
            return None
        reason = f"实际改动为{actual.label}档，超出当前的{current.label}档"
        if self.route.lane is Lane.FAST:
            self.service._upgrade(self.issue_id, self.route, actual, reason, lane=Lane.STANDARD)
            self.tracker.mark(6, BLOCKED, f"{reason}，A 通道转 B 通道", blocker=f"fix plan {self.issue_id}")
            return self._stop(HandoffStatus.BLOCKED, f"{reason}：转 B 通道，复现测试保留；下一步 fix plan {self.issue_id}")
        self.route = self.service._upgrade(self.issue_id, self.route, actual, reason)
        return None

    def _results(self, report: checks.CheckReport, changes: Sequence[diff_rules.ChangedFile],
                 others: Sequence[RegressionOutcome]) -> str:
        """第 7 步：写 result.md，返回给评审的结果文字。失败日志精简后给模型，完整输出在日志文件中。"""
        deps = self.deps
        counted = self._counted(changes)
        lines = sum(item.lines_added + item.lines_removed for item in counted)
        checked = []
        for run in self.project_runs:
            log_ref = f"日志 {deps.layout.relative(run.log) if not self.service.output_mode else run.log}"
            summary = log_ref
            if not run.passed and not run.not_run and run.log.is_file():
                summary = f"{log_ref}\n{output_trim.trim(run.log.read_text(encoding='utf-8', errors='replace'))}"
            checked.append({"name": run.command,
                            "verdict": "passed" if run.passed else ("skipped" if run.not_run else "failed"),
                            "summary": summary})
        checked += [{"name": f"复现检查 {item.check.check_id}", "verdict": _verdict(item.result), "summary": item.detail}
                    for item in self.own]
        checked += [{"name": f"Issue {item.check.issue_id} 的 {item.check.check_id}", "verdict": _verdict(item.result),
                     "summary": item.detail} for item in others]
        outputs = [f"`{item.path}`(+{item.lines_added} -{item.lines_removed})" for item in changes]
        outputs.append(f"合计 {len(counted)} 个文件、{lines} 行(不含测试文件)")
        findings = [item.text() for item in report.findings] + list(report.not_run)
        document = documents.result(self.writer, deps.clock.now(), passed=report.passed, done="运行复现测试、全部项目检查与"
                                    "相关的其他复现检查，统计相对基准版本的改动", outputs=outputs, checks=checked,
                                    deviations=findings)
        self.writer.write(documents.RESULT, document)
        text = document_files.render(document, deps.config.language, deps.zone)
        return sections.extract(text, ("conclusion", "outputs", "evidence", "deviations"))

    def _review_document(self, number: int, found: review.Review) -> None:
        self.writer.write(documents.review_name(number, found.mode.value), documents.review(
            self.writer, self.deps.clock.now(), found.mode.value, found.output if found.status is RunnerStatus.OK
            else None, found.kept, found.error))

    def _execution_findings(self) -> tuple[list[Finding], ReviewCategory] | None:
        execution = self.execution
        if execution.status is RunnerStatus.GUARD_VIOLATION:
            return ([Finding(f"guard:{item.kind.value}", item.path, item.detail, ReviewCategory.NEEDS_USER)
                     for item in execution.violations] or
                    [Finding("guard", None, "执行器报告边界违规", ReviewCategory.NEEDS_USER)], ReviewCategory.NEEDS_USER)
        if execution.status is not RunnerStatus.OK:
            return [Finding("executor", None, f"fix-executor 返回 {execution.status.value}({execution.error})",
                            ReviewCategory.LOCAL)], ReviewCategory.LOCAL
        if execution.aborted:
            big = execution.output["bigIssue"]
            return [Finding("executor", "、".join(big["locations"]) or None, f"实施中止：{big['description']}",
                            ReviewCategory.PLAN_GAP)], ReviewCategory.PLAN_GAP
        return None

    def _checks(self, number: int, commands: Sequence[project_checks.CheckCommand]):
        """第 6 步的程序检查与第 7 步的结果：复现测试先按登记副本放回(被改动或删除时记局部问题)，项目检查全量运行；
        复现测试随修复提交，但不参与针对修复改动的规则。A 通道的计划文件是预估，不检查计划外文件。"""
        deps = self.deps
        base = self.review_base
        directory = self.service.regression_dir(self.issue_id)
        restored = manifest.place_tests(directory, self.worktree)
        self.repro_tests = manifest.placed(directory, self.worktree)
        all_changes = diff_rules.collect_changes(deps.git, self.worktree, base)
        changes = [item for item in all_changes if item.path not in self.repro_tests]
        untracked = set(deps.git.untracked(self.worktree)) - self.repro_tests
        changed = [item.path for item in all_changes]
        round_dir = self.service.fix_dir(self.issue_id) / "rounds" / str(number)
        runs = project_checks.run(
            commands, self.worktree, changed, deps.launcher, round_dir / "checks",
            timeout=project_checks.timeout_seconds(deps.config),
            state=lambda root: project_checks.file_state(root, deps.git.status(root).changed_paths),
            environ=deps.environ, full=True)
        self.own, others = self._worktree_checks(changed)
        reproduction = set() if self.service.output_mode else diff_rules.reproduction_literals(
            deps.layout, self.issue_id, self.settings.min_literal_length)
        planned = self.plan if self.route.lane is not Lane.FAST else {**self.plan, "files": [
            {"path": item.path, "isNew": False} for item in changes]}
        report = checks.evaluate(CheckInputs(
            changes, untracked, planned, self.settings, runs,
            [item["path"] for item in self.plan["protectedTouches"]], reproduction, self.own, others, restored))
        self.project_runs = runs
        self.changes = all_changes
        return report, changes, report_step.patch_text(deps.git, self.worktree, base), others

    def _checkpoint(self, checkpoint: Checkpoint, number: int, report: checks.CheckReport, rollback_at: int, *,
                    last: bool) -> str:
        """第 7 步之后按本轮结果记检查点或退回检查点，返回交给下一轮的退回说明(没有退回时为空)。
        最后一轮之后不再修改，不退回，worktree 保留该轮的改动供待决定时查看。"""
        if report.not_run:
            return ""
        repro_passed = bool(self.own) and all(item.result is RegressionResult.PASSED for item in self.own)
        changed = [item.path for item in self.changes]
        findings = len(report.findings)
        if not last and checkpoint.should_rollback(repro_passed, findings, rollback_at):
            reason = "复现检查重新失败" if not repro_passed else f"有 {findings} 项不通过"
            note = checkpoint.rollback(changed, f"第 {number} 轮{reason}")
            self.tracker.mark(7, FAILED, note)
            return note
        if checkpoint.should_save(repro_passed, findings):
            checkpoint.save(number, changed, findings)
        return ""

    def _worktree_checks(self, changed: Sequence[str]) -> tuple[list[RegressionOutcome], list[RegressionOutcome]]:
        """在 worktree 上执行的复现检查(静态类与测试类)：本 Issue 的全部，其他 Issue 中最近通过且针对改动文件的。"""
        deps = self.deps
        if deps.executor is None or self.service.output_mode:
            return [], []
        target = ProbeTarget("local", self.run_.id, deps.layout.raw_dir(self.run_.id), deps.clock,
                             worktree=self.worktree)
        own = [check for check in regressions.find(deps.conn, issue_id=self.issue_id) if check.kind in WORKTREE_KINDS]
        related = [check for check in regressions.find(deps.conn, last_result=RegressionResult.PASSED)
                   if check.issue_id != self.issue_id and check.kind in WORKTREE_KINDS
                   and self._related(check, changed)]
        return (deps.executor.run_checks(own, target) if own else [],
                deps.executor.run_checks(related, target) if related else [])

    def _related(self, check: Any, changed: Sequence[str]) -> bool:
        try:
            entry = manifest.load(self.deps.layout.regression_dir(check.issue_id)).entry(check.check_id)
        except manifest.ManifestInvalid:
            return False
        return entry is not None and bool(set(entry.code_paths()) & set(changed))

    def _record(self, number: int, report: checks.CheckReport, risk: FixRisk | None,
                reviewed: Sequence[review.Review], findings: Sequence[Finding]) -> None:
        item = {"round": number, "checksPassed": report.passed,
                "reviews": [{"mode": found.mode.value, "passed": found.passed} for found in reviewed],
                "blockerCategories": list(dict.fromkeys(finding.category.value for finding in findings)),
                "risk": None if risk is None else risk.to_dict(),
                "failures": [finding.to_dict() for finding in findings]
                + [{"check": "not-run", "location": None, "problem": text, "category": ReviewCategory.NEEDS_USER.value}
                   for text in report.not_run],
                "discardedFindings": [entry for found in reviewed for entry in found.discarded]}
        self.rounds.append(item)
        atomic.write_text(self.service.fix_dir(self.issue_id) / "rounds" / str(number) / "round.json",
                          json.dumps(item, ensure_ascii=False, indent=2) + "\n")

    def _handle(self, findings: Sequence[Finding], category: ReviewCategory, last: bool,
                patch: str) -> FixResult | None:
        """返回 None 表示交回 fix-executor 修改后再来一轮。"""
        text = "；".join(item.text() for item in findings)
        if category is ReviewCategory.DESIGN:
            return self._held("设计问题", text)
        if category is ReviewCategory.NEEDS_USER:
            return self._stop(HandoffStatus.BLOCKED, f"规范要求用户确认：{text}；用 fix plan {self.issue_id} --note "
                              f"<决定> 纳入新计划，或 fix abandon {self.issue_id}")
        if category is ReviewCategory.PLAN_GAP:
            return self._replan(text)
        if last:
            return self._held("修正与复评超过上限", text)
        return None

    def _replan(self, text: str) -> FixResult:
        directory = self.service.fix_dir(self.issue_id)
        replans = len(list(directory.glob("plan.*.json")))
        if replans >= self.deps.config.whole_threshold("fix.planRounds"):
            return self._held("修复计划重出超过上限", f"计划没覆盖：{text}")
        if self.route.lane is Lane.FAST:
            self.service._upgrade(self.issue_id, self.route, self.route.tier or SizeTier.SMALL,
                                  f"实施发现计划没覆盖：{text}", lane=Lane.STANDARD)
        self.run_.end(HandoffStatus.BLOCKED)
        return self.service.plan(self.issue_id, plan_gap=f"上一次实施发现计划没覆盖：{text}")

    def _outputs(self, risk: FixRisk | None, others: Sequence[RegressionOutcome]) -> dict[str, Any]:
        deps = self.deps
        execution_output = (self.execution.output if self.execution is not None else None) or {}
        confirmation = self.confirmation or {}
        own = {item.check.check_id: item.result.value for item in self.own}
        repro_checks = regressions.find(deps.conn, issue_id=self.issue_id) if not self.service.output_mode else []
        changes = self.changes
        return {
            **self.base,
            "plan": {"path": str(self.service.fix_dir(self.issue_id) / plan_gate.PLAN),
                     "sha256": plan_gate.sha256(self.service.fix_dir(self.issue_id) / plan_gate.PLAN),
                     "operationId": confirmation.get("operationId"), "confirmedAt": confirmation.get("confirmedAt")},
            "reproCheck": [{"checkId": check.check_id, "kind": check.kind.value, "hash": check.hash,
                            "afterFix": own.get(check.check_id, RegressionResult.NOT_RUN.value)
                            if check.kind in WORKTREE_KINDS else RegressionResult.NOT_RUN.value}
                           for check in repro_checks],
            "changedFiles": [{"path": item.path, "added": item.lines_added, "removed": item.lines_removed}
                             for item in changes],
            "checks": [{"name": run.name, "command": run.command, "exitCode": run.exit_code,
                        "log": deps.layout.relative(run.log) if not self.service.output_mode else str(run.log)}
                       for run in self.project_runs],
            "planFiles": [item["path"] for item in self.plan["files"]],
            "otherRegressions": [{"issueId": item.check.issue_id, "checkId": item.check.check_id,
                                  "kind": item.check.kind.value, "result": item.result.value} for item in others],
            "risk": {"plan": self.plan.get("risk"), "apply": None if risk is None else risk.to_dict()},
            "rounds": self.rounds, "discardedFindings": self.discarded,
            "summary": self.plan["summary"], "userVisibleChange": self.plan["userVisibleChange"],
            "release": execution_output.get("release"),
            "affectedEndpoints": list(dict.fromkeys(self.plan["affectedEndpoints"])),
            "affectedPages": list(dict.fromkeys(self.plan["affectedPages"])), "migration": self.plan["migration"],
            "deviations": list(execution_output.get("deviations") or []),
            "unverified": [{"item": item["command"], "reason": "fix-executor 未运行"}
                           for item in execution_output.get("verification") or [] if item["output"] == "未运行"],
            "leftovers": list(execution_output.get("outOfScope") or []),
            "incidentalFindings": list(execution_output.get("incidental") or []),
            "protectedTouches": self.plan["protectedTouches"], "split": self.plan.get("split"),
            "lane": self.route.lane.value if self.route.lane is not None else None,
            "tier": self.route.tier.value if self.route.tier is not None else None,
        }

    def _stop(self, status: HandoffStatus, reason: str) -> FixResult:
        path = self.run_.handoff(STAGE, self.issue_id, status, self._outputs(None, []),
                                 "按说明处理后重新执行 fix apply", reason)
        self.run_.end(status)
        return FixResult(self.issue_id, status, reason, path)

    def _held(self, reason: str, details: str) -> FixResult:
        self.service._hold(self.issue_id, reason, details)
        path = self.run_.handoff(STAGE, self.issue_id, HandoffStatus.FAILED, self._outputs(None, []), "转人工",
                                 f"{reason}：{details}")
        self.run_.end(HandoffStatus.FAILED)
        return FixResult(self.issue_id, HandoffStatus.FAILED, f"{reason}：{details}", path)

    def _await_review(self, number: int, risk: FixRisk, reviewed: review.Review, patch: str,
                      others: Sequence[RegressionOutcome]) -> FixResult:
        outputs = self._outputs(risk, others)
        unknown = "；".join(f"{item['itemId']}：{item['reason']}" for item in reviewed.unknown)
        atomic.write_text(self.service.fix_dir(self.issue_id) / REVIEW_STATE, json.dumps(
            {"outputs": outputs, "reviewItems": reviewed.output["items"], "patch": patch, "round": number,
             "at": format_iso(self.deps.clock.now())}, ensure_ascii=False, indent=2) + "\n")
        reason = (f"评审无法判断：{unknown}；用 fix confirm {self.issue_id} --note <判断> 视为通过，"
                  f"或 fix confirm {self.issue_id} --reject --note <要求> 重出计划")
        self.tracker.mark(8, BLOCKED, "评审无法判断", blocker=f"fix confirm {self.issue_id} --note <判断>")
        path = self.run_.handoff(STAGE, self.issue_id, HandoffStatus.BLOCKED, outputs, "等待用户判断评审结论", reason)
        self.run_.end(HandoffStatus.BLOCKED)
        return FixResult(self.issue_id, HandoffStatus.BLOCKED, reason, path)


def _verdict(result: RegressionResult) -> str:
    if result is RegressionResult.PASSED:
        return "passed"
    return "failed" if result is RegressionResult.FAILED else "skipped"
