"""VerifyService(redesign/06-verify.md)：PR 阶段的本机检查(local、screenshots)与部署后确认(staging)。

验证只做独立于写代码模型的检查。修复前复现、项目检查与修复后复现在修复内部完成(修复第 5、7 步)，这里不再重复：
- PR 阶段(verify local)：其他问题的回归采用修复第 7 步的结果；改动涉及接口且配置了本机启动(local-run)时启动服务，
  运行接口与页面类复现检查和受影响接口的 api-fuzz 浅跑；改动涉及前端且配置了页面检查时以 page 模式启动，运行页面巡检
  并交 fix-reviewer 做截图评审；条件都不满足时不启动服务。项目 CI 的必需检查在 PR 创建后由发布跟踪读取(自动合并的条件)。
- 部署后确认(verify staging)：按问题来源由程序确认(steps/confirm.py)；全部通过时 Issue 转为最终完成，有回归时先提撤销
  PR(幂等)再退回待修，其余等待下次运行。
每次执行生成新的报告目录 data/verify/<编号>/<日期>-<阶段>/，交接文档为 verify-<阶段>-<编号>.json；--output 模式照常启动
服务，报告与交接文档写到输出目录，不改 Issue 状态、不写数据库。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import (
    CheckResult,
    HandoffStatus,
    IssueEvent,
    IssuePhase,
    OperationKind,
    OperationStatus,
    RegressionKind,
    RegressionResult,
    RunStage,
    Stage,
    VerifyPhase,
)
from tightrein.domain.issue import Hold, IssueContext, in_phase
from tightrein.extensions.client import MODE_API, MODE_PAGE, local_run_ports
from tightrein.observability.events import EventLog
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.issue.steps import transitions
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.runner.roles import run as run_role
from tightrein.pipeline.common.stage_runs import StageRun
from tightrein.pipeline.verify.prompts import screenshot_review
from tightrein.pipeline.verify.render import issue_history
from tightrein.pipeline.verify.render import report as report_render
from tightrein.pipeline.verify.steps import confirm, migration, regression, run_case, scope, screenshots, verdict
from tightrein.pipeline.verify.steps import staging as staging_step
from tightrein.pipeline.verify.steps.local_run import LocalService
from tightrein.pipeline.verify.steps.regression import Screenshot
from tightrein.pipeline.verify.steps.verdict import Item
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.procs import Launcher
from tightrein.pipeline.checks.regressions import SERVICE_KINDS, WORKTREE_KINDS
from tightrein.pipeline.checks.regressions.runner import RegressionExecutor, RegressionOutcome
from tightrein.runner.service import Runner
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store import idempotency, locks
from tightrein.store.repos import pending_operations, problems, pulls, regressions
from tightrein.store.repos.deployments import Deployment
from tightrein.store.repos.regressions import RegressionCheck

STAGE = RunStage.VERIFY
ACTOR = "verify"
READONLY_LOCK = "readonly-worktree"
CHECK_KEY = "deploy-check:{issue}:{commit}:{where}"
REVERT_KEY = "revert:{issue}:{commit}"
NO_LOCAL_RUN = "没有配置本机启动(local-run)，不启动本机服务"
NO_ENDPOINTS = "改动不涉及接口"
PAGES = "pages"  # ProbesFactory 中页面运行器的键
NO_PAGES = "改动不涉及前端，或没有配置页面检查"
FROM_FIX = "修复第 7 步的结果"
LocalFactory = Callable[[Path, str, Path], LocalService]
ExecutorFactory = Callable[[ProbeTarget], RegressionExecutor]
ProbesFactory = Callable[[ProbeTarget], Mapping[str, Any]]  # 键为 api-fuzz 与 pages
Revert = Callable[[str, str], Any]


@dataclass(frozen=True)
class VerifyResult:
    issue_id: str
    status: HandoffStatus
    conclusion: str | None
    message: str
    handoff: Path | None = None
    report: Path | None = None


@dataclass
class VerifyDeps:
    """local 为空表示项目没有配置本机启动；page_checks 为真表示配置了页面检查(page-routes 或工作区的 e2e 用例)；
    revert 为部署后确认发现回归时提撤销 PR 的函数(组装为 ReleaseService.revert)。"""

    layout: WorkspaceLayout
    tool: ToolLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    runner: Runner
    git: Any
    launcher: Launcher
    executor: ExecutorFactory | None = None
    local: LocalFactory | None = None
    probes: ProbesFactory | None = None
    extensions: Any = None
    operations: Any = None
    sync: Callable[[str | None], str] | None = None
    environ: Mapping[str, str] = field(default_factory=dict)
    zone: tzinfo | None = None
    page_checks: bool = False
    revert: Revert | None = None


@dataclass
class _Local:
    """一次 PR 阶段检查中收集的检查项与其他结果。"""

    items: list[Item] = field(default_factory=list)
    unverified: list[dict[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    shots: list[Screenshot] = field(default_factory=list)
    migration: dict[str, Any] | None = None
    target: dict[str, str] = field(default_factory=lambda: {"url": "worktree", "mode": "worktree"})
    logs: list[str] = field(default_factory=list)

    def skip(self, item_id: str, category: str, reason: str) -> None:
        self.items.append(Item(item_id, category, CheckResult.UNVERIFIED, reason=reason))
        self.unverified.append({"item": item_id, "reason": reason})


class VerifyService:
    def __init__(self, deps: VerifyDeps) -> None:
        self.deps = deps

    @property
    def output_mode(self) -> bool:
        return self.deps.layout.output_dir is not None

    def _env(self) -> IssueEnv:
        return IssueEnv(self.deps.conn, self.deps.layout, self.deps.clock, self.deps.config, self.deps.zone)

    def _issue(self, issue_id: str) -> Any:
        return transitions.record_of(self._env(), issue_id).issue

    def _event(self, issue_id: str, event: IssueEvent, issue_context: IssueContext = IssueContext(), *,
               note: str | None = None) -> None:
        if not self.output_mode:
            transitions.apply_event(self._env(), transitions.record_of(self._env(), issue_id), event, issue_context,
                                    actor=ACTOR, note=note)

    def _fix_outputs(self, issue_id: str) -> tuple[str, dict[str, Any]] | None:
        return stage_runs.latest_outputs(self.deps.conn, self.deps.layout, RunStage.FIX, issue_id)

    def _report_dir(self, issue_id: str, phase: VerifyPhase) -> Path:
        if self.output_mode:
            return self.deps.layout.output_path("verify", phase.value)
        return self.deps.layout.verify_dir(issue_id, self.deps.clock.now().date(), phase)

    def _target(self, run: StageRun, report_dir: Path, worktree: Path, commit: str, url: str | None) -> ProbeTarget:
        return ProbeTarget("local", run.id, report_dir / "raw", self.deps.clock, base_url=url, release=commit,
                           worktree=worktree)

    def _checks(self, issue_id: str) -> list[RegressionCheck]:
        return regressions.find(self.deps.conn, issue_id=issue_id)

    def _run_checks(self, checks: Sequence[RegressionCheck], local: LocalService, run: StageRun,
                    report_dir: Path, worktree: Path, commit: str) -> list[RegressionOutcome]:
        """接口与页面类复现检查：需要 local 中所需的服务都已就绪，否则记为未执行。"""
        deps = self.deps
        if not checks:
            return []
        entries = {entry.id: entry for entry in scope.entries_of(deps.layout.regression_dir(checks[0].issue_id),
                                                                 checks)}
        outcomes: list[RegressionOutcome] = []
        for check in checks:
            entry = entries.get(check.check_id)
            if entry is None:
                outcomes.append(RegressionOutcome(check, RegressionResult.INVALID, check.check_id, "清单中没有该检查"))
                continue
            reason = local.unverified(entry.requires)
            if reason is not None:
                outcomes.append(RegressionOutcome(check, RegressionResult.NOT_RUN, entry.location, reason))
                continue
            if deps.executor is None:
                outcomes.append(RegressionOutcome(check, RegressionResult.NOT_RUN, entry.location,
                                                  "没有提供复现检查执行器"))
                continue
            ports = local_run_ports(deps.config, local.mode)
            port = ports["frontend"] if check.kind is RegressionKind.PAGE else ports["backend"]
            url = None if port is None else f"http://localhost:{port}"
            target = self._target(run, report_dir, worktree, commit, url)
            outcomes += deps.executor(target).run_checks([check], target)
        return outcomes

    def _write(self, run: StageRun, issue_id: str, phase: VerifyPhase, status: HandoffStatus, outputs: dict[str, Any],
               next_action: str, reason: str | None, report_dir: Path, logs: Sequence[str] = (),
               history: bool = True) -> VerifyResult:
        path = run.handoff(STAGE, issue_id, status, outputs, next_action, reason, phase=phase)
        document = json.loads(path.read_text(encoding="utf-8"))
        report = report_render.write(report_dir / "report.md", document, logs)
        if history and not self.output_mode:
            transitions.annotate(self._env(), issue_id, issue_history.verify_line(outputs))
        run.end(status)
        return VerifyResult(issue_id, status, outputs["conclusion"], reason or next_action, path, report)

    def _record_results(self, outcomes: Sequence[RegressionOutcome], run_id: str, release: str | None) -> None:
        if self.output_mode:
            return
        for outcome in outcomes:
            regressions.record_result(self.deps.conn, outcome.check.issue_id, outcome.check.check_id,
                                      outcome.result, run_id, self.deps.clock.now(), release)

    def _local_url(self, mode: str) -> str:
        """本机服务的地址；没有配置端口(项目没有 local-run 扩展)时为 localhost。"""
        port = (lambda ports: ports["frontend"] or ports["backend"])(local_run_ports(self.deps.config, mode))
        return "localhost" if port is None else f"http://localhost:{port}"

    # PR 阶段

    def local(self, issue_id: str, *, confirm_migration: bool = False) -> VerifyResult:
        deps = self.deps
        fix = self._fix_outputs(issue_id)
        worktree = deps.layout.fix_worktree(issue_id)
        if not self.output_mode and not in_phase(self._issue(issue_id), IssuePhase.VERIFY):
            return VerifyResult(issue_id, HandoffStatus.BLOCKED, None, f"先执行 fix done {issue_id}")
        if fix is None or fix[0] != HandoffStatus.OK.value:
            return VerifyResult(issue_id, HandoffStatus.BLOCKED, None, f"修复没有通过评审，先执行 fix apply {issue_id}")
        outputs = fix[1]
        base = stage_runs.review_base(deps.conn, deps.layout, issue_id, outputs["baseCommit"])
        if deps.git.diff_hash(worktree, base) != outputs["diffHash"]:
            return VerifyResult(issue_id, HandoffStatus.BLOCKED, None,
                                f"工作区改动与修复评审时不一致，先执行 fix apply {issue_id} --review-only")
        return _LocalRun(self, issue_id, outputs, worktree).run(confirm_migration)

    def screenshots(self, issue_id: str, *, ok: bool, note: str | None = None) -> VerifyResult:
        deps = self.deps
        found = stage_runs.latest_outputs(deps.conn, deps.layout, STAGE, issue_id, VerifyPhase.LOCAL)
        waiting = found is not None and found[1]["conclusion"] == verdict.AWAITING and any(
            item["category"] == verdict.SCREENSHOT and item["result"] == CheckResult.UNVERIFIED.value
            for item in found[1]["items"])
        if not waiting:
            return VerifyResult(issue_id, HandoffStatus.BLOCKED, None, "最近一次合并前验证不是在等待截图查看")
        outputs = dict(found[1])
        items = screenshots.user_verdict([_item(item) for item in outputs["items"]], ok, note)
        run = stage_runs.begin(STAGE, deps.layout, deps.conn, deps.clock, deps.events, outputs["commit"])
        return self._conclude(run, issue_id, outputs, items, None, self._report_dir(issue_id, VerifyPhase.LOCAL), [])

    def _conclude(self, run: StageRun, issue_id: str, outputs: dict[str, Any], items: Sequence[Item],
                  awaiting: str | None, report_dir: Path, logs: Sequence[str]) -> VerifyResult:
        """awaiting 为等待用户的原因(迁移待确认、截图待查看)；有失败项时照常判为失败。"""
        deps = self.deps
        conclusion = verdict.local_conclusion(items, awaiting is not None)
        previous = stage_runs.latest_outputs(deps.conn, deps.layout, STAGE, issue_id, VerifyPhase.LOCAL)
        count = previous[1].get("consecutiveFailures", 0) if previous is not None else 0
        failures = count + 1 if conclusion == verdict.FAILED else (count if conclusion == verdict.AWAITING else 0)
        outputs = {**outputs, "items": [item.to_dict() for item in items], "conclusion": conclusion,
                   "consecutiveFailures": failures}
        if conclusion == verdict.AWAITING:
            return self._write(run, issue_id, VerifyPhase.LOCAL, HandoffStatus.BLOCKED, outputs, "等待用户",
                               awaiting, report_dir, logs)
        if conclusion == verdict.PASSED:
            self._event(issue_id, IssueEvent.VERIFY_PASSED, note=report_render.summary(outputs))
            return self._write(run, issue_id, VerifyPhase.LOCAL, HandoffStatus.OK, outputs,
                               f"提交与提 PR：release {issue_id}", None, report_dir, logs)
        first = verdict.first_failure(items)
        reason = f"合并前验证失败：{first.id} {first.reason or ''}".rstrip() if first else "合并前验证失败"
        maximum = deps.config.whole_threshold("verify.maxConsecutiveFailures")
        context = IssueContext()
        if failures >= maximum:
            context = IssueContext(hold=Hold(f"合并前验证连续 {failures} 次失败", Stage.VERIFY, deps.clock.now(), reason))
        self._event(issue_id, IssueEvent.VERIFY_FAILED, context, note=reason)
        return self._write(run, issue_id, VerifyPhase.LOCAL, HandoffStatus.OK, outputs,
                           f"退回修复：fix start {issue_id}", None, report_dir, logs)

    # 部署后确认

    def staging(self, issue_id: str) -> VerifyResult:
        deps = self.deps
        if not self.output_mode and not in_phase(self._issue(issue_id), IssuePhase.DEPLOY_CHECK):
            return VerifyResult(issue_id, HandoffStatus.BLOCKED, None, f"Issue {issue_id} 不在等待部署后确认")
        pull = pulls.get(deps.conn, issue_id)
        if pull is None or pull.merge_commit is None:
            return VerifyResult(issue_id, HandoffStatus.BLOCKED, None, "没有 PR 的合并提交，先执行 release track")
        deployment = staging_step.deployment_for(deps.conn, deps.git, deps.config.repo, pull.merge_commit)
        if deployment is None:
            return VerifyResult(issue_id, HandoffStatus.BLOCKED, None, "还没有包含合并提交的成功部署(没有配置部署来源时，"
                                "release track 在合并后经过观察期按已部署记录)")
        return _DeployCheck(self, issue_id, deployment, pull.merge_commit).run()


def _item(data: Mapping[str, Any]) -> Item:
    return Item(data["id"], data["category"], CheckResult(data["result"]), data["command"], tuple(data["evidence"]),
                data["reason"])


def _from_fix(result: str) -> CheckResult:
    if result == RegressionResult.PASSED.value:
        return CheckResult.PASS
    return CheckResult.FAIL if result == RegressionResult.FAILED.value else CheckResult.UNVERIFIED


class _LocalRun:
    """一次 PR 阶段的本机检查：其他问题的回归(修复的结果) → 按条件启动本机服务的接口测试与截图评审。"""

    def __init__(self, service: VerifyService, issue_id: str, fix: Mapping[str, Any], worktree: Path) -> None:
        self.service = service
        self.deps = service.deps
        self.issue_id = issue_id
        self.fix = fix
        self.worktree = worktree
        self.base = fix["baseCommit"]
        self.commit = self.deps.git.head(worktree).commit or self.base
        self.run_ = stage_runs.begin(STAGE, self.deps.layout, self.deps.conn, self.deps.clock, self.deps.events,
                                     self.commit)
        self.report_dir = service._report_dir(issue_id, VerifyPhase.LOCAL)
        self.state = _Local()
        diff = self.deps.git.diff(worktree, self.base)
        self.changed = [*diff.paths, *self.deps.git.untracked(worktree)]
        self.added = {item.path: list(item.added_lines) for item in diff.files}

    def outputs(self) -> dict[str, Any]:
        return {"issueId": self.issue_id, "phase": VerifyPhase.LOCAL.value, "target": self.state.target,
                "commit": self.commit, "baseCommit": self.base, "unverified": self.state.unverified,
                "skipped": self.state.skipped, "migration": self.state.migration}

    def conclude(self, awaiting: str | None = None) -> VerifyResult:
        return self.service._conclude(self.run_, self.issue_id, self.outputs(), self.state.items, awaiting,
                                      self.report_dir, self.state.logs)

    def run(self, confirm_migration: bool) -> VerifyResult:
        state = self.state
        self._other_regressions()
        checks = [check for check in self.service._checks(self.issue_id) if check.kind in SERVICE_KINDS]
        endpoints_output, routes_output = self._extension_outputs()
        found = scope.compute(self.fix, self.changed, endpoints_output, routes_output)
        wants_api = bool(found.endpoints)
        wants_page = bool(found.pages) and self.deps.page_checks
        if self.deps.local is None or not (wants_api or wants_page):
            state.skipped.append(NO_LOCAL_RUN if self.deps.local is None else f"{NO_ENDPOINTS}；{NO_PAGES}")
            return self.conclude()
        if not wants_api:
            state.skipped.append(f"接口测试：{NO_ENDPOINTS}")
        if not wants_page:
            state.skipped.append(f"截图评审：{NO_PAGES}")
            found = scope.Scope(found.endpoints, (), found.changed)
        related = [check for check in scope.related_regressions(self.deps.conn, self.deps.layout, self.issue_id, found)
                   if check.kind in SERVICE_KINDS]
        mode = MODE_PAGE if wants_page else MODE_API
        local = self.deps.local(self.worktree, mode, self.report_dir)
        pending = self._migration_pending(local, confirm_migration)
        if pending is not None:
            return self.conclude(f"迁移待确认({pending})：确认后执行 verify local {self.issue_id} --confirm-migration")
        with local as running:
            fallback = running.occupied and mode != MODE_API
            if not fallback:
                self._verify(checks, related, found, running)
                self._finish_local(running, mode)
        if not fallback:
            return self._screenshots()
        state.skip("page-checks", verdict.PAGE_PATROL, "page 模式的端口被占用，页面类检查未验证；空出端口后重跑")
        with self.deps.local(self.worktree, MODE_API, self.report_dir) as api:
            self._verify([check for check in checks if check.kind is not RegressionKind.PAGE],
                         [check for check in related if check.kind is not RegressionKind.PAGE],
                         scope.Scope(found.endpoints, (), found.changed), api)
            self._finish_local(api, MODE_API)
        return self.conclude()

    def _other_regressions(self) -> None:
        """修复第 7 步已在修复 worktree 上运行相关的其他复现检查(静态类与测试类)，这里采用其结果，不重跑。"""
        for item in self.fix.get("otherRegressions") or []:
            result = _from_fix(item["result"])
            item_id = f"other:{item['issueId']}/{item['checkId']}"
            self.state.items.append(Item(item_id, verdict.OTHER_REPRO, result, reason=FROM_FIX))
            if result is CheckResult.UNVERIFIED:
                self.state.unverified.append({"item": item_id, "reason": f"{FROM_FIX}：{item['result']}"})

    def _finish_local(self, local: LocalService, mode: str) -> None:
        self.state.logs = [str(item.log) for item in local.services.values()]
        self.state.target = {"url": self.service._local_url(mode), "mode": mode}

    def _extension_outputs(self) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        client = self.deps.extensions
        if client is None:
            return None, None
        endpoints = client.authz_endpoints(self.worktree, self.commit)
        routes = client.page_routes(self.worktree, self.commit)
        return endpoints.output, routes.output

    def _migration_pending(self, local: LocalService, confirm_migration: bool) -> str | None:
        """迁移待确认时返回操作编号；没有迁移改动或已确认时为空。"""
        deps = self.deps
        files = migration.changed(self.changed, local.migration_paths)
        if not files:
            self.state.migration = {"changed": False, "entries": [], "operationId": None}
            return None
        sha = migration.files_hash(self.worktree, files)
        entries = migration.entries(self.added, files)
        if confirm_migration and not migration.confirmed(deps.conn, self.issue_id, sha):
            for record in pending_operations.find(deps.conn, subject_id=self.issue_id, status=OperationStatus.PENDING):
                if record.kind is OperationKind.LOCAL_MIGRATION and record.preconditions.get("migrationSha256") == sha:
                    deps.operations.confirm(record.id, confirmed_by="user", clock=deps.clock)
                    deps.operations.execute(record.id, clock=deps.clock)
        if migration.confirmed(deps.conn, self.issue_id, sha):
            self.state.migration = {"changed": True, "entries": entries, "operationId": self._migration_op(sha)}
            return None
        operation = migration.request(deps.conn, deps.clock, repo=str(deps.config.repo), issue_id=self.issue_id,
                                      files=files, added=entries, migration=self.fix.get("migration"), sha=sha)
        self.state.migration = {"changed": True, "entries": entries, "operationId": operation.id}
        return operation.id

    def _migration_op(self, sha: str) -> str | None:
        executed = pending_operations.find(self.deps.conn, subject_id=self.issue_id, status=OperationStatus.EXECUTED)
        for record in executed:
            if record.kind is OperationKind.LOCAL_MIGRATION and record.preconditions.get("migrationSha256") == sha:
                return record.id
        return None

    def _verify(self, checks: Sequence[RegressionCheck], related: Sequence[RegressionCheck], found: scope.Scope,
                local: LocalService) -> None:
        deps = self.deps
        state = self.state
        outcomes = self.service._run_checks(checks, local, self.run_, self.report_dir, self.worktree, self.commit)
        state.items += [Item(f"repro:{item.check.check_id}", verdict.ISSUE_REPRO, verdict.from_outcome(item), None,
                             item.artifacts, item.detail) for item in outcomes]
        others: list[RegressionOutcome] = []
        for issue_id in dict.fromkeys(check.issue_id for check in related):
            group = [check for check in related if check.issue_id == issue_id]
            others += self.service._run_checks(group, local, self.run_, self.report_dir, self.worktree, self.commit)
        state.items += [Item(f"other:{item.check.issue_id}/{item.check.check_id}", verdict.OTHER_REPRO,
                             verdict.from_outcome(item), None, item.artifacts, item.detail) for item in others]
        state.unverified += [{"item": f"复现检查 {item.check.issue_id}/{item.check.check_id}", "reason": item.detail}
                             for item in [*outcomes, *others] if item.result is not RegressionResult.PASSED
                             and item.result is not RegressionResult.FAILED]
        self.service._record_results([*outcomes, *others], self.run_.id, self.commit)
        backend = local_run_ports(deps.config, local.mode)["backend"]
        url = None if backend is None else f"http://localhost:{backend}"
        probes = deps.probes(self._probe_target(url)) if deps.probes is not None and url else {}
        existing = regression.existing_problems(deps.conn, deps.layout, self.service._issue(self.issue_id).problems)
        if found.endpoints:
            shallow = regression.shallow(probes.get("api-fuzz"), self._probe_target(url), found.endpoints, existing)
            if shallow is not None:
                state.items.append(shallow)
        if found.pages and local.mode != MODE_API:
            page_url = self.service._local_url(local.mode)
            page_probes = deps.probes(self._probe_target(page_url)) if deps.probes is not None else {}
            patrol, shots = regression.patrol(page_probes.get(PAGES), self._probe_target(page_url), found.pages)
            if patrol is not None:
                state.items.append(patrol)
            state.shots = shots

    def _probe_target(self, url: str | None) -> ProbeTarget:
        return self.service._target(self.run_, self.report_dir, self.worktree, self.commit, url)

    def _screenshots(self) -> VerifyResult:
        deps = self.deps
        if not self.state.shots:
            return self.conclude()
        raw = self.report_dir / "raw"
        shots = [Screenshot(str((raw / item.path).relative_to(self.report_dir)), item.note)
                 for item in self.state.shots]
        result = screenshots.review(
            lambda task: run_role(deps.runner, task, deps.clock,
                                  conn=None if deps.layout.output_dir is not None else deps.conn),
            lambda items: screenshot_review.task(deps.tool, deps.config, self.run_.id, self.issue_id, self.report_dir,
                                                 items), shots)
        self.state.items += result.items
        if result.awaiting:
            return self.conclude(f"截图待查看：verify screenshots {self.issue_id} --ok 或 --issue <说明>")
        return self.conclude()


class _DeployCheck:
    """部署后确认(redesign/06-verify.md 第 2 节)：接口与页面类复现检查对目标环境重放，静态类与测试类在只读 worktree
    切到部署 commit 后重跑(两者各按「Issue + 部署 commit」幂等)；来自服务端日志或没有可执行检查的关联问题看观察期内
    是否再出现。回归时先提撤销 PR(幂等键 revert:<Issue>:<合并提交>)，再退回待修。"""

    def __init__(self, service: VerifyService, issue_id: str, deployment: Deployment, merge_commit: str) -> None:
        self.service = service
        self.deps = service.deps
        self.issue_id = issue_id
        self.deployment = deployment
        self.merge_commit = merge_commit
        self.commit = deployment.commit
        self.run_ = stage_runs.begin(STAGE, self.deps.layout, self.deps.conn, self.deps.clock, self.deps.events,
                                     self.commit)
        self.report_dir = service._report_dir(issue_id, VerifyPhase.STAGING)
        self.items: list[Item] = []
        self.confirmations: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str | None:
        return self.deps.config.base_url

    def target(self, worktree: Path | None, url: str | None) -> ProbeTarget:
        return ProbeTarget(str(self.deps.config.get("target.environment")), self.run_.id, self.report_dir / "raw",
                           self.deps.clock, base_url=url, release=self.commit, worktree=worktree)

    def _record(self, item: Item, source: str, method: str) -> None:
        self.items.append(item)
        self.confirmations.append({"object": item.id.split(":", 1)[-1], "source": source, "method": method,
                                   "result": item.result.value, "detail": item.reason})

    def _run_group(self, where: str, group: Sequence[RegressionCheck], worktree: Path | None,
                   url: str | None) -> list[dict[str, Any]]:
        deps = self.deps

        def action() -> dict[str, Any]:
            if deps.executor is None:
                return {"outcomes": [{"checkId": check.check_id, "result": RegressionResult.NOT_RUN.value,
                                      "detail": "没有提供复现检查执行器", "met": True} for check in group]}
            target = self.target(worktree, url)
            found = deps.executor(target).run_checks(group, target)
            self.service._record_results(found, self.run_.id, self.commit)
            return {"outcomes": [{"checkId": item.check.check_id, "result": item.result.value,
                                  "detail": item.detail, "met": item.precondition_met} for item in found]}

        key = CHECK_KEY.format(issue=self.issue_id, commit=self.commit, where=where)
        return list(idempotency.run_once(deps.conn, key, action, deps.clock).result["outcomes"])

    def _checks(self, group: Sequence[RegressionCheck], where: str, worktree: Path | None, url: str | None,
                method: str) -> None:
        by_id = {check.check_id: check for check in group}
        for found in self._run_group(where, group, worktree, url):
            check = by_id[found["checkId"]]
            outcome = RegressionOutcome(check, RegressionResult(found["result"]), check.check_id, found["detail"],
                                        found["met"])
            self._record(Item(f"{verdict.DEPLOY_CONFIRM}:{check.check_id}", verdict.DEPLOY_CONFIRM,
                              verdict.from_outcome(outcome), None, (), found["detail"]), check.kind.value, method)

    def _unrunnable(self, group: Sequence[RegressionCheck], method: str, reason: str) -> None:
        for check in group:
            self._record(Item(f"{verdict.DEPLOY_CONFIRM}:{check.check_id}", verdict.DEPLOY_CONFIRM,
                              CheckResult.UNVERIFIED, reason=reason), check.kind.value, method)

    def _target_checks(self, group: Sequence[RegressionCheck]) -> None:
        if not group:
            return
        if self.base_url is None:
            self._unrunnable(group, confirm.REPLAY, "没有配置被测地址(target.baseUrl)")
            return
        self._checks(group, "target", None, self.base_url, confirm.REPLAY)

    def _worktree_checks(self, group: Sequence[RegressionCheck]) -> None:
        deps = self.deps
        if not group:
            return
        if deps.sync is None:
            self._unrunnable(group, confirm.RERUN, "没有提供只读 worktree 的切换")
            return
        ttl = timedelta(minutes=deps.config.whole_threshold("verify.readonlyLockMinutes"))
        with locks.held(deps.conn, READONLY_LOCK, deps.clock, ttl):
            deps.sync(self.commit)
            self._checks(group, "worktree", deps.layout.readonly_worktree(), None, confirm.RERUN)

    def _observe(self, problem_ids: Sequence[str]) -> None:
        deps = self.deps
        deployed_at = self.deployment.deployed_at or self.deployment.detected_at
        hours = deps.config.whole_threshold("verify.observationHours")
        for problem_id in problem_ids:
            problem = problems.get(deps.conn, problem_id)
            if problem is None:
                continue
            result, reason = confirm.observe(problem.last_seen_at, deployed_at, deps.clock.now(), hours)
            self._record(Item(f"{verdict.DEPLOY_CONFIRM}:{problem_id}", verdict.DEPLOY_CONFIRM, result, reason=reason),
                         problem.probe.value, confirm.OBSERVE)

    def run(self) -> VerifyResult:
        deps = self.deps
        issue = self.service._issue(self.issue_id)
        checks = self.service._checks(self.issue_id)
        self._target_checks([check for check in checks if check.kind in SERVICE_KINDS])
        self._worktree_checks([check for check in checks if check.kind in WORKTREE_KINDS])
        sources = {problem_id: found.probe for problem_id in issue.problems
                   if (found := problems.get(deps.conn, problem_id)) is not None}
        self._observe(confirm.observed(sources, self.items))
        unverified = [{"item": item.id, "reason": item.reason or "未执行"} for item in self.items
                      if item.result is CheckResult.UNVERIFIED]
        outputs = {"issueId": self.issue_id, "phase": VerifyPhase.STAGING.value,
                   "target": {"url": self.base_url or self.deployment.url or self.commit, "mode": "staging"},
                   "commit": self.commit, "baseCommit": self.commit, "items": [item.to_dict() for item in self.items],
                   "unverified": unverified, "confirmations": self.confirmations}
        decision = confirm.decide(self.confirmations)
        if decision == confirm.REGRESSED:
            failed = verdict.first_failure(self.items)
            reason = f"部署后确认发现回归(部署 commit {self.commit[:12]})：{failed.id} {failed.reason or ''}".rstrip()
            outputs["revert"] = self._revert(reason)
            self.service._event(self.issue_id, IssueEvent.STAGING_FAILED, note=reason)
            return self._write({**outputs, "conclusion": verdict.FAILED}, HandoffStatus.OK,
                               f"撤销 PR 交用户决定；重新修复：fix start {self.issue_id}", None)
        if decision == confirm.WAITING:
            reason = "等待确认：" + "；".join(f"{item['item']}：{item['reason']}" for item in unverified)
            days = deps.config.whole_threshold("verify.stagingWaitDays")
            if staging_step.wait_exceeded(self.deployment.deployed_at, deps.clock.now(), days):
                reason += f"；部署后已超过 {days} 天"
            return self._write({**outputs, "conclusion": verdict.AWAITING}, HandoffStatus.BLOCKED,
                               "下次定时运行继续确认", reason, history=False)
        note = f"部署 commit {self.commit[:12]} 确认通过"
        if not self.service.output_mode:
            _, skipped = run_case.capture(deps.layout, deps.conn, deps.git, deps.config.repo, self.issue_id,
                                          self.commit, deps.clock.now())
            note += f"；没有保存评测用例：{skipped}" if skipped else "；已保存为评测用例"
        self.service._event(self.issue_id, IssueEvent.STAGING_VERIFIED, note=note)
        return self._write({**outputs, "conclusion": verdict.PASSED}, HandoffStatus.OK,
                           f"起草 PR 回复并由用户发起清理：fix cleanup {self.issue_id}", None)

    def _revert(self, reason: str) -> dict[str, Any] | None:
        """提撤销 PR(ReleaseService.revert，生成待确认操作)；同一 Issue 的同一个合并提交只提一次。"""
        deps = self.deps
        if deps.revert is None or self.service.output_mode:
            return None

        def action() -> dict[str, Any]:
            result = deps.revert(self.issue_id, reason)
            return {"operationId": getattr(result, "operation", None), "message": str(getattr(result, "message", ""))}

        key = REVERT_KEY.format(issue=self.issue_id, commit=self.merge_commit)
        return dict(idempotency.run_once(deps.conn, key, action, deps.clock).result)

    def _write(self, outputs: dict[str, Any], status: HandoffStatus, next_action: str, reason: str | None,
               history: bool = True) -> VerifyResult:
        return self.service._write(self.run_, self.issue_id, VerifyPhase.STAGING, status, outputs, next_action,
                                   reason, self.report_dir, (), history)
