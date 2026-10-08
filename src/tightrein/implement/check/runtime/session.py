"""本机运行检查的一次会话：同一 commit 只启动一次服务，接口浅跑、页面巡检、截图共用。

只在接入清单启用了 `implement.check.runtime`、且改动涉及接口或页面时做：
1. 算出受影响的接口与页面(都没有就不启动)；有页面用 page 模式，否则 api 模式；
2. 向项目启动脚本要启动计划；改到迁移文件时先停下等用户确认(哈希为前置条件)；
3. 启动服务；page 模式的端口被占时退回 api 模式，页面类检查记未验证并提示空出端口后重跑；
4. 接口浅跑 → 页面巡检 → 截图比对与审查；退出时停掉全部服务。
另一个本机服务正占着锁时不等待，相关检查记未验证。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.implement.check.changes import Changes
from tightrein.implement.check.runtime import api, local_run, migration, page_runner, pages, screenshots
from tightrein.implement.check.runtime.local_run import LaunchPlan, LocalRunSettings, LocalService, Spawner
from tightrein.implement.check.runtime.migration import MigrationRequest
from tightrein.implement.check.runtime.ports import PortProbe, ProcessTable, PsTable, SocketProbe
from tightrein.implement.check.runtime.verdict import Item, Result, unverified
from tightrein.implement.context import ImplementContext
from tightrein.protocol.handoff import Tokens
from tightrein.protocol.http import Transport, UrllibTransport
from tightrein.protocol.raw import RawDir
from tightrein.protocol.runtime import Runtime
from tightrein.store.locks import Busy, FileLock

MODULE = local_run.MODULE
DESIGN = "implement.design"
SERVICES_DIR = "local_run"
SERVICES_FILE = "services.json"
LOCK_BUSY = "另一个本机服务正在运行(对象锁 local-run 被占)，稍后重跑"
PORTS_OCCUPIED = "page 模式的端口被其他程序占用，已退回 api 模式；空出端口后重跑以检查页面"


@dataclass(frozen=True)
class SessionDeps:
    """外部依赖：测试整体替换。"""

    spawner: Spawner
    probe: Callable[[float], PortProbe]
    table: Callable[[Runtime], ProcessTable]
    transport: Transport


def real_deps() -> SessionDeps:
    return SessionDeps(local_run.SubprocessSpawner(), SocketProbe, lambda runtime: PsTable(runtime.runner,
                                                                                            runtime.environ),
                       UrllibTransport())


@dataclass
class RuntimeOutcome:
    items: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    migration: MigrationRequest | None = None  # 等用户确认的迁移
    awaiting_screenshots: list[str] = field(default_factory=list)
    calls: int = 0
    tokens: Tokens = field(default_factory=Tokens)

    @property
    def pending(self) -> bool:
        return self.migration is not None or bool(self.awaiting_screenshots)


@dataclass(frozen=True)
class _Scope:
    endpoints: list[str]
    pages: list[str]
    routes: list[Mapping[str, Any]]


def run(runtime: Runtime, context: ImplementContext, changes: Changes, *, raw_dir: Path,
        deps: SessionDeps | None = None) -> RuntimeOutcome:
    if not runtime.setup.enabled(MODULE) or context.worktree is None:
        return RuntimeOutcome()
    worktree = context.worktree
    design = context.last(DESIGN)
    facts = design.facts if design is not None else {}
    api_settings = api.ApiSettings.from_settings(runtime.settings)
    page_settings = page_runner.PageSettings.from_settings(runtime.settings)
    routes = pages.routes(worktree, page_settings.routes)
    scope = _Scope(api.affected_endpoints(facts, changes.paths, api.inventory(worktree, api_settings.endpoints)),
                   pages.affected_pages(facts, changes.paths, routes), routes)
    if not scope.endpoints and not scope.pages:
        return RuntimeOutcome(notes=["改动不涉及接口与页面，不做本机运行检查"])
    settings = LocalRunSettings.from_settings(runtime.settings)
    mode = local_run.MODE_PAGE if scope.pages else local_run.MODE_API
    plan = local_run.launch_plan(runtime, worktree=worktree, mode=mode, raw=RawDir(raw_dir), settings=settings)
    if isinstance(plan, str):
        return RuntimeOutcome(_all_unverified(scope, plan))
    files = migration.changed(changes.paths, plan.migration_paths)
    if files:
        request = migration.request(worktree, changes.lines, files, facts)
        if not migration.confirmed(context, request.hash):
            return RuntimeOutcome([unverified("migration", "migration", "改到了迁移文件，等用户确认后再启动服务")],
                                  migration=request)
    found = RuntimeOutcome()
    _session(runtime, context, worktree, plan, scope, settings, raw_dir, deps or real_deps(), found)
    return found


def _session(runtime: Runtime, context: ImplementContext, worktree: Path, plan: LaunchPlan, scope: _Scope,
             settings: LocalRunSettings, raw_dir: Path, deps: SessionDeps, found: RuntimeOutcome) -> None:
    try:
        with _service(runtime, worktree, plan, settings, raw_dir, deps) as service:
            if not service.occupied or plan.mode == local_run.MODE_API:
                _checks(runtime, context, worktree, service, scope, raw_dir, deps, found)
                found.notes += service.notes
                return
            found.notes += service.notes
    except Busy:
        found.items += _all_unverified(scope, LOCK_BUSY)
        return
    found.items += [unverified(f"page:{page}", pages.CATEGORY, PORTS_OCCUPIED) for page in scope.pages]
    if not scope.endpoints:
        return
    fallback = local_run.launch_plan(runtime, worktree=worktree, mode=local_run.MODE_API, raw=RawDir(raw_dir),
                                     settings=settings)
    if isinstance(fallback, str):
        found.items += [unverified(f"api:{endpoint}", api.CATEGORY, fallback) for endpoint in scope.endpoints]
        return
    _session(runtime, context, worktree, fallback, _Scope(scope.endpoints, [], scope.routes), settings, raw_dir, deps,
             found)


def _service(runtime: Runtime, worktree: Path, plan: LaunchPlan, settings: LocalRunSettings, raw_dir: Path,
             deps: SessionDeps) -> LocalService:
    lock = FileLock(runtime.workspace.object_lock(local_run.LOCK_NAME), runtime.clock, stale_s=settings.lock_stale_s)
    return LocalService(plan=plan, worktree=worktree, report_dir=raw_dir, settings=settings,
                        lock=lock, spawner=deps.spawner, probe=deps.probe(settings.port_probe_timeout_s),
                        table=deps.table(runtime), transport=deps.transport, environ=runtime.environ,
                        services_file=runtime.workspace.data_dir / SERVICES_DIR / SERVICES_FILE)


def _checks(runtime: Runtime, context: ImplementContext, worktree: Path, service: LocalService, scope: _Scope,
            raw_dir: Path, deps: SessionDeps, found: RuntimeOutcome) -> None:
    item = api.shallow(scope.endpoints, base_url=service.base_url("api"), unavailable=service.unverified("api"),
                       worktree=worktree, report_dir=raw_dir / "api", settings=api.ApiSettings.from_settings(
                           runtime.settings), runner=runtime.runner, environ=runtime.environ,
                       token=runtime.secrets.get(api.SECRET_TOKEN, ""), transport=deps.transport,
                       known=api.existing(runtime.conn, context.issue.id))
    if item is not None:
        found.items.append(item)
    if not scope.pages:
        return
    base_url = service.base_url("pages")
    if base_url is None:
        found.items += [unverified(f"page:{page}", pages.CATEGORY, service.unverified("pages") or "页面服务未就绪")
                        for page in scope.pages]
        return
    page_dir = raw_dir / "pages"
    page_run = page_runner.run(base_url=base_url, settings=page_runner.PageSettings.from_settings(runtime.settings),
                               secrets=runtime.secrets, e2e_dir=pages.e2e_dir(runtime.workspace), raw_dir=page_dir,
                               runner=runtime.runner, environ=runtime.environ, redactor=runtime.redactor)
    patrol, shots = pages.judge(page_run, scope.pages, scope.routes, (str(page_dir / page_runner.RESULTS_FILE),))
    found.items.append(patrol)
    if patrol.result is Result.UNVERIFIED:
        return
    _screenshots(runtime, context, shots, page_dir, found)


def _screenshots(runtime: Runtime, context: ImplementContext, shots: Sequence[pages.Screenshot], raw_dir: Path,
                 found: RuntimeOutcome) -> None:
    reviewed = screenshots.reviewed_dir(runtime.workspace, context.issue.id)
    references = [reviewed]
    if context.base_commit:
        references.insert(0, screenshots.baseline_dir(runtime.workspace, context.base_commit))
    result = screenshots.review(runtime, context, shots, raw_dir=raw_dir, references=references,
                                settings=screenshots.CompareSettings.from_settings(runtime.settings))
    screenshots.remember(shots, result.items, reviewed)
    found.items += result.items
    found.awaiting_screenshots += result.awaiting
    found.calls += result.calls
    found.tokens.add(result.tokens)


def _all_unverified(scope: _Scope, reason: str) -> list[Item]:
    return [*(unverified(f"api:{endpoint}", api.CATEGORY, reason) for endpoint in scope.endpoints),
            *(unverified(f"page:{page}", pages.CATEGORY, reason) for page in scope.pages)]
