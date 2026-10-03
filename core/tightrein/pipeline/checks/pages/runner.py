"""PageRunner：验证环节的页面巡检与页面类复现检查共用的 Playwright 运行器。

步骤：检查 Playwright 的安装 → 读取各角色的账号名与密码 → 在新建的临时目录(0700)中放登录态 → 写计划文件 →
调用 Playwright → 删除登录态目录 → 解析 results.ndjson → 清除 trace 中的凭证 → 整理页面上的失败。
- 角色取 sources/common/session.probe_roles：没有配置 accounts 时以匿名身份(anonymous)运行，不读钥匙串、没有 setup 项目；
  accounts.login.kind 为 static-header 时账号名为角色名，密码即凭证，同样经工作区 e2e/login.ts 在页面上登录；
- 账号取不到的角色与 setup 失败的角色写入说明，状态为 partial；全部角色都不可用、没有任何用例得到执行为 failed；
- 结果文件缺失或最后一行不完整为 failed；超时终止后已写出的结果照常解析，状态为 partial；
- 没有目标地址时为 skipped。
spec_dirs 为空时运行工作区 e2e/ 下的巡检用例(checks.pages.patrolGrep 过滤)，给出时只运行这些目录中的用例(复现检查)。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.config.secrets import SecretError
from tightrein.domain.enums import RunStatus
from tightrein.pipeline.checks.pages import artifacts, failures, invoke, plan, result_parser
from tightrein.pipeline.checks.pages.failures import PageFailure
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.procs import Launcher, ToolRun
from tightrein.sources.common.raw import RawDir
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.session import ANONYMOUS_ROLE, NO_TARGET, Credentials, probe_roles
from tightrein.store.files.layout import WorkspaceLayout

AUTH_DIR_PREFIX = "tightrein-pages-auth-"
AUTH_DIR_MODE = 0o700
INCOMPLETE = "结果不完整：results.ndjson 缺失或最后一行不完整"


@dataclass(frozen=True)
class PageRun:
    status: RunStatus
    failures: tuple[PageFailure, ...] = ()
    artifacts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageDependencies:
    config: ProjectConfig
    credentials: Credentials
    launcher: Launcher
    layout: WorkspaceLayout
    redactor: ProbeRedactor
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    installed_problem: Callable[[], str | None] = invoke.installed_problem
    runtime: Path = invoke.RUNTIME_DIR
    temp_root: Path | None = None


class PageRunner:
    def __init__(self, dependencies: PageDependencies) -> None:
        self.deps = dependencies

    def run(self, target: ProbeTarget, *, roles: Sequence[str] = (), spec_dirs: Sequence[Path] = (),
            grep: str | None = None) -> PageRun:
        if target.base_url is None:
            return PageRun(RunStatus.SKIPPED, notes=(NO_TARGET,))
        problem = self.deps.installed_problem()
        if problem is not None:
            return PageRun(RunStatus.FAILED, notes=(problem,))
        accounts, passwords, unavailable = self._credentials(probe_roles(self.deps.config, tuple(roles)))
        notes = [f"角色 {role}：{reason}" for role, reason in unavailable.items()]
        if not accounts:
            return PageRun(RunStatus.FAILED, notes=(*notes, "全部角色的账号都不可用"))
        raw = RawDir(target.raw_dir)
        raw.ensure()
        auth_dir = Path(tempfile.mkdtemp(prefix=AUTH_DIR_PREFIX, dir=self.deps.temp_root))
        auth_dir.chmod(AUTH_DIR_MODE)
        try:
            document = plan.from_config(self.deps.config, base_url=target.base_url, accounts=accounts,
                                        workspace_e2e=self.deps.layout.e2e_dir(), auth_dir=auth_dir,
                                        raw_dir=target.raw_dir, spec_dirs=spec_dirs)
            plan_path = plan.write(raw.path(plan.PLAN_FILE), document)
            tool_run = invoke.run(self.deps.launcher, plan_path, plan.selected_projects(document),
                                  plan.grep_for(self.deps.config, grep), None, passwords, self.deps.environ,
                                  invoke.timeout_seconds(self.deps.config), raw.path(invoke.LOG_FILE),
                                  self.deps.runtime)
        finally:
            shutil.rmtree(auth_dir, ignore_errors=True)
        return self._outcome(target, raw, bool(unavailable), tool_run, notes)

    def _credentials(self, roles: tuple[str, ...]) -> tuple[dict[str, str | None], dict[str, str], dict[str, str]]:
        """各角色的账号名(匿名身份为空)、密码与取不到账号的原因。"""
        accounts: dict[str, str | None] = {}
        passwords: dict[str, str] = {}
        unavailable: dict[str, str] = {}
        for role in roles:
            if role == ANONYMOUS_ROLE:
                accounts[role] = None
                continue
            try:
                accounts[role] = self.deps.credentials.account(role)
                passwords[role] = self.deps.credentials.password(role)
            except SecretError as error:
                accounts.pop(role, None)
                unavailable[role] = str(error)
        return accounts, passwords, unavailable

    def _outcome(self, target: ProbeTarget, raw: RawDir, degraded: bool, tool_run: ToolRun,
                 notes: list[str]) -> PageRun:
        if not tool_run.started:
            return PageRun(RunStatus.FAILED, notes=(*notes, f"Playwright {tool_run.describe()}"))
        results = result_parser.parse(raw.path(plan.RESULTS_FILE))
        if not results.complete and not tool_run.timed_out:
            return PageRun(RunStatus.FAILED, artifacts=raw.files(), notes=(*notes, INCOMPLETE))
        notes += artifacts.clean_traces(target.raw_dir, self.deps.redactor)
        found = failures.collect(results)
        notes += [f"角色 {role} 登录失败：{error}" for role, error in found.failed_setups.items()]
        if tool_run.timed_out:
            notes.append("Playwright 超过 checks.pages.timeoutMinutes，已终止，已写出的结果照常解析")
        if results.unknown:
            notes.append(f"结果文件中有 {results.unknown} 行无法解析，已跳过")
        if not found.executed_roles:
            status = RunStatus.FAILED
            notes.append("没有任何角色的用例得到执行")
        elif degraded or found.failed_setups or tool_run.timed_out:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.OK
        cleaned = tuple(replace(item, message=self.deps.redactor.text(item.message)) for item in found.failures)
        return PageRun(status, cleaned, raw.files(), tuple(notes))
