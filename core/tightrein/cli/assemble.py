"""组装根(architecture/09 第 2 节)：打开工作区，把外部依赖接到各模块的 Deps，构造各模块的 service。

这是全仓库唯一接触真实外部依赖的地方：git 与 gh 的进程、HTTP、钥匙串、agent 工具的进程、扩展进程、本机服务的
进程与端口、本机通知命令。它们集中在 Externals 中，测试整体替换；其余构造都是纯粹的对象组装。
App 同时实现编排层的 Modules 协议。各 service 按需构造并缓存；修复与发布的 follow_up 登记在同一个
OperationRunner 上，确认执行建分支、提交等操作后由发起模块记录结果。
--output 模式下工作区布局带输出目录，各模块按自己的约定不写数据库；数据库照常打开供读取。
"""

from __future__ import annotations

import functools
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time as day_time, timedelta, tzinfo
from pathlib import Path
from typing import Any, TextIO

from tightrein.cli.exit_codes import UsageError
from tightrein.config import network, project, user
from tightrein.config.network import Reroute
from tightrein.config.project import ProjectConfig
from tightrein.config.secrets import Keychain, SecretError
from tightrein.config.secrets import run_command as secret_command
from tightrein.config.user import UserConfig
from tightrein.domain import ids
from tightrein.domain.clock import Clock, FixedClock, SystemClock, parse_iso
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import ExtensionPoint, RunStage, Stage
from tightrein.evaluation import rubric
from tightrein.evaluation import service as evaluation
from tightrein.evaluation.cases import ModuleCase, verify_cases
from tightrein.domain.signal import Signal
from tightrein.extensions import commands as ext_commands
from tightrein.extensions.cache import ExtensionCache
from tightrein.extensions.client import MODE_API, MODE_PAGE, ExtensionClient, local_run_ports
from tightrein.extensions.invoke import Invoker, ProcessRunner, SubprocessRunner
from tightrein.extensions.resolve import Resolution, resolve
from tightrein.guards.policy import GuardSettings
from tightrein.guards.service import Guards
from tightrein.observability.events import EventLog
from tightrein.observability.notify import CommandRunner, Notifier
from tightrein.observability.notify import run_command as notify_command
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer
from tightrein.packaging import third_party
from tightrein.packaging.third_party import AlternateRoute
from tightrein.pipeline.checks import project_checks
from tightrein.pipeline.aggregate.manual import ProblemCommands
from tightrein.pipeline.aggregate.service import AggregateDeps, AggregateService
from tightrein.pipeline.collect.prompts.static_reviewer import StaticReviewer
from tightrein.pipeline.collect.service import CollectDeps, CollectService
from tightrein.pipeline.collect.steps import preconditions
from tightrein.pipeline.fix.service import FixDeps, FixService
from tightrein.orchestrator.onboarding.service import Onboarding, OnboardingDeps
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.pipeline.issue.steps import github_comments
from tightrein.pipeline.issue.steps.github import GithubMirror
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.pipeline.improve.service import ImproveDeps, ImproveService
from tightrein.pipeline.learn.service import LearnDeps, LearnService
from tightrein.pipeline.learn.steps.rule_check import RuleCheckSettings
from tightrein.pipeline.learn.steps.rules import RuleEnv
from tightrein.pipeline.learn.steps.third_party import RepoFacts
from tightrein.pipeline.release.service import ReleaseDeps, ReleaseService
from tightrein.runner.roles import Overrides
from tightrein.pipeline.triage.service import TriageDeps, TriageService
from tightrein.pipeline.verify.service import PAGES, VerifyDeps, VerifyService
from tightrein.pipeline.verify.steps.local_run import LocalService, Spawner, SubprocessSpawner
from tightrein.pipeline.verify.steps.ports import PortProbe, ProcessTable, PsTable, SocketProbe
from tightrein.sources.api_fuzz.probe import ApiFuzzDependencies, ApiFuzzProbe
from tightrein.sources.api_fuzz.replay import signal_replayer
from tightrein.sources.base import Probe, ProbeTarget
from tightrein.sources.common import target as common_target
from tightrein.sources.common.http import Transport, UrllibTransport
from tightrein.sources.common.procs import Launcher
from tightrein.sources.common.procs import SubprocessLauncher as ToolLauncher
from tightrein.sources.common.raw import RawDir
from tightrein.sources.common.redact import ProbeLimits, ProbeRedactor
from tightrein.sources.common.session import KeychainCredentials, LoginSettings, Session
from tightrein.sources.incidental.probe import IncidentalDependencies, IncidentalProbe
from tightrein.pipeline.checks.regressions.api_check import ApiCheck
from tightrein.pipeline.checks.pages.runner import PageDependencies, PageRunner
from tightrein.pipeline.checks.regressions.page_check import PageCheck
from tightrein.pipeline.checks.regressions.repo_test_check import RepoTestCheck
from tightrein.pipeline.checks.regressions.runner import RegressionExecutor
from tightrein.pipeline.checks.regressions.static_check import StaticCheck
from tightrein.sources import enabled as source_enabled
from tightrein.sources.access_log.source import AccessLogDependencies, AccessLogSource
from tightrein.sources.alerts.source import AlertsDependencies, AlertsSource
from tightrein.sources.platform_errors.source import PlatformErrorsDependencies, PlatformErrorsSource
from tightrein.sources.project_probes.source import ProjectProbeDependencies, ProjectProbeSource
from tightrein.sources.static.probe import StaticDependencies, StaticProbe
from tightrein.sources.static.tools import semgrep
from tightrein.retrieval.service import KnowledgeService, RetrievalSettings
from tightrein.runner.adapters.replay import ReplayAdapter
from tightrein.runner.process import ProcessLauncher
from tightrein.runner.process import SubprocessLauncher as AgentLauncher
from tightrein.runner.recording import RecordingSet
from tightrein.runner.registry import Registry, default_adapters
from tightrein.runner.service import Runner
from tightrein.store import locks, retention
from tightrein.store.files.layout import ToolLayout, UserLayout, WorkspaceLayout
from tightrein.store.migrations.runner import open_database
from tightrein.vcs import worktrees
from tightrein.vcs.errors import RefNotFound, VcsError
from tightrein.vcs.executor import OperationRunner
from tightrein.vcs.gh_issues import GhIssues
from tightrein.pipeline.common.deploys import DeploySource
from tightrein.vcs.gh_read import GhReader
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.operations import OperationPlanner
from tightrein.vcs.process import Executor, VcsProcess, subprocess_executor

LOCAL_RUN_MODES = (MODE_API, MODE_PAGE)
REROUTE_DECISION = "network-reroute"


@dataclass
class Externals:
    """全部外部依赖；测试整体替换。environ 为当前进程的环境，transport 为空时按本机用户配置的代理生成
    (environment_for、transport_for)。"""

    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    home: Path = field(default_factory=Path.home)
    tool_root: Path | None = None
    zone: tzinfo | None = None
    vcs_execute: Executor = subprocess_executor
    transport: Transport | None = None
    notify_run: CommandRunner | None = None
    secret_run: Callable[..., Any] = secret_command
    agent_launcher: ProcessLauncher | None = None
    tool_launcher: Launcher | None = None
    extension_runner: ProcessRunner | None = None
    spawner: Spawner = field(default_factory=SubprocessSpawner)
    port_probe: PortProbe | None = None
    process_table: ProcessTable = field(default_factory=PsTable)
    which: Callable[[str], str | None] = shutil.which
    stdin_is_tty: Callable[[], bool] = field(default_factory=lambda: sys.stdin.isatty)
    sleep: Callable[[float], None] = time.sleep
    alive: Callable[[int], bool] = locks.process_alive
    edit_file: Callable[[Path], None] = field(default_factory=lambda: open_in_editor)
    randomness: Callable[[int], bytes] = os.urandom
    program: Path = field(default_factory=lambda: Path(sys.executable).with_name("tightrein"))

    def tool(self) -> ToolLayout:
        """本工具仓库；tool_root 为空时取核心所在仓库。"""
        return ToolLayout(self.tool_root) if self.tool_root is not None else ToolLayout()


@dataclass(frozen=True)
class Options:
    """通用参数(architecture/09 4.2)中影响组装的部分。"""

    workspace: Path | None = None
    now: str | None = None
    output_dir: Path | None = None
    runner: str | None = None
    model: str | None = None
    replay_from: str | None = None
    target: str | None = None
    commit: str | None = None


def parse_now(text: str, zone: tzinfo | None) -> datetime:
    """`--now`：带时区的 ISO 时间，或只有日期(取该日本机时区的 00:00)。"""
    try:
        if "T" in text:
            return parse_iso(text)
        day = date.fromisoformat(text)
    except ValueError as error:
        raise UsageError(f"--now 须为带时区的 ISO 时间或日期：{text}") from error
    moment = datetime.combine(day, day_time(), zone) if zone is not None else datetime.combine(day, day_time()).astimezone()
    return moment


def open_in_editor(path: Path) -> None:
    """用 $EDITOR(没有时为 vi)打开文件，等待编辑器退出。"""
    subprocess.run([os.environ.get("EDITOR", "vi"), str(path)], check=False)


def environment_for(ext: Externals, config: UserConfig) -> dict[str, str]:
    """全部子进程共用的环境：当前进程的环境加本机用户配置的代理(config.network)。"""
    return network.environ(ext.environ, config)


def transport_for(ext: Externals, config: UserConfig, environment: Mapping[str, str]) -> Transport:
    """核心自己的 HTTP 请求：测试替身优先；配置了代理时按同一份环境的代理表发送。"""
    if ext.transport is not None:
        return ext.transport
    return UrllibTransport(proxies=network.proxies(environment) if config.network_proxy is not None else None)


def alternate_route(ext: Externals, config: ProjectConfig, environment: Mapping[str, str],
                    record: Callable[[Reroute], None]) -> AlternateRoute | None:
    """第三方 skill 下载遇到网络类错误时的另一条路；使用测试替身或环境中没有代理地址时没有。"""
    host = str(config.get("runtime.network.rerouteHost"))
    switched = network.rerouted(environment, host)
    if switched is None or ext.transport is not None:
        return None
    return AlternateRoute(UrllibTransport(proxies=network.proxies(switched)), network.route(environment, host),
                          network.route(switched, host), tuple(config.get("runtime.network.errorPatterns")), record)


def resolve_workspace(given: Path, workspaces: Path) -> Path:
    """工作区可以写路径，也可以写项目名(本工具 workspaces/ 下的目录名)。"""
    if given.is_dir():
        return given
    named = workspaces / given.name
    if len(given.parts) == 1 and named.is_dir():
        return named
    known = sorted(item.name for item in workspaces.iterdir() if item.is_dir()) if workspaces.is_dir() else []
    raise UsageError(f"工作区不存在：{given}" + (f"；已有的工作区：{'、'.join(known)}" if known else ""))


class App:
    def __init__(self, options: Options, externals: Externals | None = None) -> None:
        self.options = options
        self.externals = externals or Externals()
        ext = self.externals
        self.home = ext.home
        self.zone = ext.zone
        self.tool = ext.tool()
        self.user: UserConfig = user.load(home=ext.home)
        workspace = options.workspace or self.user.default_workspace
        if workspace is None:
            raise UsageError("没有给出 --workspace，本机用户配置中也没有 defaultWorkspace")
        workspace = resolve_workspace(workspace, self.tool.workspaces_dir())
        self.root = workspace.resolve()
        self.layout = WorkspaceLayout(self.root, options.output_dir)
        self.config: ProjectConfig = project.load(self.layout.project_config(), self.tool.root,
                                                   routing=self.user.routing)
        self.clock: Clock = FixedClock(parse_now(options.now, self.zone)) if options.now else SystemClock()
        self.redactor = Redactor()
        self.redactor.register(network.proxy_password(self.user) or "")
        self.environ: dict[str, str] = environment_for(ext, self.user)
        self.transport: Transport = transport_for(ext, self.user, self.environ)
        self.probe_redactor = ProbeRedactor(self.redactor, ProbeLimits.from_config(self.config))
        self.events = EventLog(self.layout, self.redactor)
        self.tracer = Tracer(self.events, self.clock, run_id=None)
        self.conn: sqlite3.Connection = open_database(WorkspaceLayout(self.root).database(), self.clock,
                                                     int(self.config.get("runtime.store.busyTimeoutMs")))
        self.session_id = ids.run_id(self.clock.now(), RunStage.LOOP)
        self.process = VcsProcess(execute=ext.vcs_execute, environ=dict(self.environ), sleep=ext.sleep,
                                  redactor=self.redactor, timeout=float(self.config.get("runtime.vcs.timeoutSeconds")),
                                  fetch_timeout=float(self.config.get("runtime.vcs.fetchTimeoutSeconds")),
                                  retry_delays=tuple(self.config.get("runtime.vcs.readRetryDelaysSeconds")),
                                  stderr_tail_lines=int(self.config.get("runtime.vcs.stderrTailLines")),
                                  reroute_host=str(self.config.get("runtime.network.rerouteHost")),
                                  network_patterns=tuple(self.config.get("runtime.network.errorPatterns")),
                                  on_reroute=self._rerouted)
        self.git = GitReader(self.process)
        self.gh = GhReader(self.process)
        self.deploy_source = DeploySource(lambda: self.extensions(), self.git, self.config.repo)
        method = self.user.notify_method or self.config.get("notify.method")
        notify_run = ext.notify_run or functools.partial(
            notify_command, timeout_seconds=float(self.config.get("runtime.observability.notifyTimeoutSeconds")))
        self.notifier = Notifier(self.conn, method, self.clock, self.redactor, run=notify_run, zone=self.zone)
        self.overrides = Overrides(options.runner, options.model)
        configured = self.user.tool_path(semgrep.TOOL)
        self.semgrep = semgrep.resolve_command(
            str(configured) if configured is not None else str(self.config.get("runtime.tools.semgrep")),
            self.tool.root)
        self.stderr: TextIO = sys.stderr
        self._cache: dict[str, Any] = {}

    # 通用

    def _rerouted(self, reroute: Reroute) -> None:
        """git、gh 换路重试：写 gate 事件；运行摘要从 process.reroutes 列出。"""
        self.tracer.event("gate", decision=REROUTE_DECISION, reason=reroute.text(), attributes=reroute.to_dict())

    @property
    def output_mode(self) -> bool:
        return self.layout.output_dir is not None

    def close(self) -> None:
        self.conn.close()

    def _once(self, key: str, build: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    def wait_until(self, moment: datetime) -> None:
        """等到 moment：固定时钟(--now)直接拨快，系统时钟睡眠；编排用它保证同一秒内不开始两个运行。"""
        delta = moment - self.clock.now()
        if delta <= timedelta(0):
            return
        if isinstance(self.clock, FixedClock):
            self.clock.advance(delta)
        else:
            self.externals.sleep(delta.total_seconds())

    def base_url(self) -> str | None:
        """--target 或 target.baseUrl；都没有时为空，需要地址的探针返回 skipped。"""
        return self.options.target or self.config.base_url

    # 共用的依赖

    def knowledge(self) -> KnowledgeService:
        return self._once("knowledge", lambda: KnowledgeService(
            self.layout, self.conn, self.clock, self.tracer, settings=RetrievalSettings.from_config(self.config),
            sandbox=self.output_mode, zone=self.zone))

    def runner(self) -> Runner:
        def build() -> Runner:
            replay = None
            if self.options.replay_from is not None:
                replay = ReplayAdapter(RecordingSet(self._replay_dir(self.options.replay_from)), self.process)
            guards = Guards(self.git, GuardSettings.from_config(self.config), self.layout, self.tool,
                            tracer=self.tracer)
            return Runner(conn=self.conn, layout=self.layout, tool_layout=self.tool, config=self.config,
                          registry=self._registry(), guards=guards, launcher=self._agent_launcher(),
                          tracer=self.tracer, redactor=self.redactor, environ=dict(self.environ), zone=self.zone,
                          replay=replay)

        return self._once("runner", build)

    def runner_for(self, output_dir: Path) -> Runner:
        """评测评分用的执行器：写入评测的输出目录，不写工作区。"""
        layout = WorkspaceLayout(self.root, output_dir)
        guards = Guards(self.git, GuardSettings.from_config(self.config), layout, self.tool, tracer=self.tracer)
        return Runner(conn=self.conn, layout=layout, tool_layout=self.tool, config=self.config,
                      registry=self._registry(), guards=guards, launcher=self._agent_launcher(), tracer=self.tracer,
                      redactor=self.redactor, environ=dict(self.environ), zone=self.zone)

    def _registry(self) -> Registry:
        skew = timedelta(seconds=float(self.config.get("runtime.runner.sessionClockSkewSeconds")))
        return Registry(default_adapters(self.home, skew), self.user, which=self.externals.which)

    def _agent_launcher(self) -> ProcessLauncher:
        return self.externals.agent_launcher or AgentLauncher.from_config(self.config)

    def _replay_dir(self, given: str) -> Path:
        """--replay-from：录制集目录，或运行编号(取该运行的目录)。"""
        path = Path(given)
        if path.is_dir():
            return path
        try:
            if ids.kind_of(given) == "run":
                return WorkspaceLayout(self.root).run_dir(given)
        except ValueError:
            pass
        raise UsageError(f"--replay-from 既不是目录也不是运行编号：{given}")

    def extension_runner(self) -> ProcessRunner:
        return self.externals.extension_runner or SubprocessRunner.from_config(self.config)

    def tool_launcher(self) -> Launcher:
        """探针与项目检查启动外部工具；输出上限与终止宽限同扩展进程(runtime.extensions.*)。"""
        return self.externals.tool_launcher or ToolLauncher(self.extension_runner())

    def resolution(self) -> Resolution:
        return self._once("resolution", lambda: resolve(self.config, self.layout, self.tool))

    def invoker(self, run_id: str | None = None) -> Invoker:
        return Invoker(self.layout, UserLayout(self.home), run_id=run_id or self.session_id,
                       runner=self.extension_runner(), environ=dict(self.environ), redactor=self.redactor,
                       tracer=self.tracer, stderr_tail_lines=int(self.config.get("runtime.extensions.stderrTailLines")),
                       tools={semgrep.COMMAND_ENV: self.semgrep})

    def extensions(self, run_id: str | None = None) -> ExtensionClient:
        return ExtensionClient(self.config, self.resolution(), self.invoker(run_id),
                               ExtensionCache(self.layout, self.clock), head=lambda repo: self.git.head(repo).commit)

    def keychain(self) -> Keychain:
        return Keychain(self.redactor.register, run=self.externals.secret_run,
                        timeout_seconds=float(self.config.get("runtime.keychain.timeoutSeconds")))

    def credentials(self) -> KeychainCredentials:
        return KeychainCredentials(self.config, self.keychain())

    def session(self, base_url: str | None, timeout_seconds: float | None = None) -> Session:
        timeout = timeout_seconds if timeout_seconds is not None else float(
            self.config.get("regressions.loginTimeoutSeconds"))
        return Session(base_url, LoginSettings.from_config(self.config), self.credentials(), self.transport,
                       self.probe_redactor, timeout_seconds=timeout)

    def deploys(self) -> DeploySource:
        return self.deploy_source

    def sync_readonly(self, commit: str | None = None) -> str:
        """切换前先恢复持有进程已不存在的只读锁定(模型调用被强行结束时遗留)；持有进程仍在的照常报锁定。"""
        Guards(self.git, GuardSettings.from_config(self.config), self.layout, self.tool, tracer=self.tracer,
               alive=self.externals.alive).recover()
        return worktrees.sync_readonly(self.git, self.layout, commit, self.config.main_branch).commit

    def onboarding(self) -> Onboarding:
        """接入流程：试连接走 ext run 的同一路径，检查命令在切到主分支的只读 worktree 上全量运行。"""
        def load() -> ProjectConfig:
            return project.load(self.layout.project_config(), self.tool.root, routing=self.user.routing)

        def try_point(config: ProjectConfig, point: ExtensionPoint) -> Any:
            head = self.git.head(config.repo).commit
            return ext_commands.run_point(self.invoker(), resolve(config, self.layout, self.tool), point,
                                          ext_commands.default_input(point, config, self.layout, self.session_id),
                                          repo=config.repo, commit=head)

        def run_checks(config: ProjectConfig) -> list[str]:
            try:
                self.sync_readonly()
            except (VcsError, OSError) as error:
                return [f"无法把只读 worktree 切到主分支(先执行 tightrein project worktree init)：{error}"]
            runs = project_checks.run(project_checks.commands(config), self.layout.readonly_worktree(), [],
                                      self.tool_launcher(), self.layout.onboarding_checks_dir(),
                                      timeout=project_checks.timeout_seconds(config), state=lambda root: {},
                                      environ=dict(self.environ), full=True)
            return [f"{item.command}(退出码 {item.exit_code}，日志 {self.layout.relative(item.log)})"
                    if not item.not_run else f"{item.command}：{item.not_run_reason}" for item in runs if not item.passed]

        def health(config: ProjectConfig) -> str | None:
            result = preconditions.health(config, str(config.base_url), self.transport)
            return None if result.ok else f"状态码 {result.status}"

        def account(item: str) -> str | None:
            try:
                self.keychain().read_account(item)
            except SecretError as error:
                return str(error)
            return None

        return self._once("onboarding", lambda: Onboarding(OnboardingDeps(
            self.conn, self.layout, self.clock, load, git=self.git, try_point=try_point, run_checks=run_checks,
            health=health, account=account, zone=self.zone)))

    def main_head(self) -> str | None:
        self.git.fetch(self.config.repo)
        try:
            return self.git.rev_parse(self.config.repo, f"origin/{self.config.main_branch}")
        except RefNotFound:
            return None

    def planner(self, run_id: str | None = None) -> OperationPlanner:
        return OperationPlanner(self.conn, self.git, self.gh, self.layout, self.config, self.clock,
                                run_id or self.session_id)

    def operations(self, run_id: str | None = None) -> OperationRunner:
        """待确认操作的执行入口；修复与发布登记了 follow_up，有 GitHub 镜像时 issue 也登记(镜像操作执行后记下结果)。"""
        follow_ups = {Stage.FIX: self.fix().follow_up(), Stage.RELEASE: self.release().follow_up()}
        mirror = self.github_mirror()
        if mirror is not None:
            follow_ups[Stage.ISSUE] = mirror.follow_up()
        return OperationRunner(self.conn, self.process, self.git, self.gh, self.layout, run_id or self.session_id,
                               tracer=self.tracer, follow_ups=follow_ups,
                               expire_days=int(self.config.get("runtime.vcs.operationExpireDays")))

    # 探针与复现检查

    def probe(self, kind: ProbeKind, base_url: str | None = None) -> Probe:
        """base_url 为接口类探针登录的目标地址，缺省为 staging(或 --target)。"""
        ext = self.externals
        environ = dict(self.environ)
        if kind is ProbeKind.API_FUZZ:
            session = self.session(base_url or self.base_url())
            return ApiFuzzProbe(ApiFuzzDependencies(self.config, self.extensions(), session,
                                                    self.tool_launcher(), self.layout, self.probe_redactor, environ,
                                                    ext.randomness))
        release_at = self.release_at
        if kind is ProbeKind.PLATFORM_ERRORS:
            return PlatformErrorsSource(PlatformErrorsDependencies(self.config, self.extensions(), self.conn,
                                                                   self.probe_redactor, release_at, ext.randomness))
        if kind is ProbeKind.ACCESS_LOG:
            return AccessLogSource(AccessLogDependencies(self.config, self.extensions(), self.conn,
                                                         self.probe_redactor, release_at, ext.randomness))
        if kind is ProbeKind.ALERTS:
            return AlertsSource(AlertsDependencies(self.config, self.extensions(), self.probe_redactor, release_at,
                                                   ext.randomness))
        if kind is ProbeKind.PROJECT_PROBE:
            return self.project_probes()
        if kind is ProbeKind.STATIC:
            return StaticProbe(StaticDependencies(self.config, self.extensions(), self.git, self.tool_launcher(),
                                                  self.probe_redactor, environ, ext.randomness, self.semgrep,
                                                  self.layout.rules_dir()))
        return IncidentalProbe(IncidentalDependencies(self.conn, self.layout, self.probe_redactor, ext.randomness))

    def release_at(self, at: datetime) -> str | None:
        """发生时间之前最近一次成功部署的 commit(平台来源与项目探针的信号版本)。"""
        return common_target.release_at(self.conn, at)

    def project_probes(self) -> ProjectProbeSource:
        return ProjectProbeSource(ProjectProbeDependencies(
            self.config, self.conn, self.layout.root, self.probe_redactor, self.release_at,
            environ=dict(self.environ), randomness=self.externals.randomness))

    def disabled_sources(self) -> dict[str, str]:
        """未启用的采集方法与原因，写进运行摘要。"""
        return source_enabled.disabled(self.config, self.extensions().configured)

    def pages(self) -> PageRunner:
        """验证环节的页面运行器(页面巡检、截图与页面类复现检查)。"""
        return PageRunner(PageDependencies(self.config, self.credentials(), self.tool_launcher(), self.layout,
                                           self.probe_redactor, dict(self.environ)))

    def regression_executor(self, base_url: str | None = None) -> RegressionExecutor:
        """复现检查执行器：接口类按目标地址登录，页面类用页面运行器，静态类用 Semgrep，测试类用项目检查命令。"""
        url = base_url or self.base_url()
        api = ApiCheck(self.session(url), self.transport, self.probe_redactor,
                       float(self.config.get("regressions.apiTimeoutSeconds")))
        static = StaticCheck(self.tool_launcher(), dict(self.environ),
                             float(self.config.get("regressions.staticTimeoutSeconds")), self.semgrep)
        commands = project_checks.commands(self.config)
        test = RepoTestCheck.configured(self.tool_launcher(), dict(self.environ), self.config,
                                        lambda command, file: project_checks.repro_test_cwd(commands, command, file))
        return RegressionExecutor(self.layout, api=api, page=PageCheck(self.pages()), static=static,
                                  test=test)

    def reviewer(self, run_id: str) -> StaticReviewer:
        return StaticReviewer(runner=self.runner(), tool=self.tool, layout=self.layout, config=self.config,
                              conn=self.conn, clock=self.clock, run_id=run_id,
                              workdir=self.layout.readonly_worktree(), context=self.knowledge().context_for,
                              overrides=self.overrides)

    def replayer(self) -> Callable[[Signal, int], list[bool | None]]:
        root = WorkspaceLayout(self.root)
        target = ProbeTarget(str(self.config.get("target.environment")),
                             ids.run_id(self.clock.now(), RunStage.AGGREGATE), root.data_dir(), self.clock,
                             base_url=self.base_url())
        return signal_replayer(target, self.session(self.base_url()), self.transport, self.probe_redactor,
                               lambda signal: RawDir(root.probe_raw_dir(signal.run_id, signal.probe)),
                               float(self.config.get("regressions.apiTimeoutSeconds")))

    # 各模块

    def collect(self) -> CollectService:
        return self._once("collect", lambda: CollectService(CollectDeps(
            self.layout, self.config, self.conn, self.clock, self.events, self.probe, self.transport,
            self.deploy_source, self.git, self.probe_redactor, regressions=self.regression_executor(),
            reviewer=self.reviewer,
            randomness=self.externals.randomness, disabled=self.disabled_sources)))

    def aggregate(self) -> AggregateService:
        return self._once("aggregate", lambda: AggregateService(AggregateDeps(
            self.layout, self.config, self.conn, self.clock, self.events, self.git, replayer=self.replayer(),
            sleep=self.externals.sleep)))

    def problems(self) -> ProblemCommands:
        return ProblemCommands(self.layout, self.config, self.conn, self.clock, self.events, self.externals.sleep)

    def triage(self) -> TriageService:
        return self._once("triage", lambda: TriageService(TriageDeps(
            self.layout, self.tool, self.config, self.conn, self.clock, self.events, self.runner(), self.git,
            self.sync_readonly, prs=self.gh, context=self.knowledge().context_for, zone=self.zone)))

    def github_mirror(self) -> GithubMirror | None:
        """issues.tracker 为 github 时的 GitHub Issue 镜像；gh 经同一个 VcsProcess(超时、不接终端、代理环境)。"""
        if not github_comments.enabled(self.config):
            return None
        return self._once("github-mirror", lambda: GithubMirror(
            IssueEnv(self.conn, self.layout, self.clock, self.config, self.zone), self.process,
            GhIssues(self.process, self.config.repo, int(self.config.get("runtime.vcs.issueListLimit"))),
            self.git.remotes, self.redactor, self.session_id, self.planner))

    def issue(self) -> IssueService:
        return self._once("issue", lambda: IssueService(IssueDeps(
            self.layout, self.config, self.conn, self.clock, self.events, self.notifier,
            snapshot=self.layout.readonly_worktree(), zone=self.zone, mirror=self.github_mirror(),
            prepare=lambda issue_id: self.fix().prepare(issue_id))))

    def _endpoints(self, worktree: Path) -> Mapping[str, Any] | None:
        head = self.git.head(worktree).commit
        return self.extensions().authz_endpoints(worktree, head).output if head else None

    def _services(self, worktree: Path) -> Mapping[str, list[str]]:
        """local-run 各模式要启动的服务名(复现检查的 requires)。"""
        client = self.extensions()
        found: dict[str, list[str]] = {}
        for mode in LOCAL_RUN_MODES:
            output = client.local_run(worktree, mode, local_run_ports(self.config, mode)).output or {}
            found[mode] = [item["name"] for item in output.get("services", [])]
        return found

    def fix(self) -> FixService:
        def build() -> FixService:
            service = FixService(FixDeps(
                self.layout, self.tool, self.config, self.conn, self.clock, self.events, self.runner(), self.git,
                self.tool_launcher(), planner=self.planner(), executor=self.regression_executor(),
                branch_prefix=self.user.branch_prefix, context=self.knowledge().context_for,
                endpoints=self._endpoints, services=self._services, environ=dict(self.environ), zone=self.zone,
                overrides=self.overrides))
            return service

        created = "fix" not in self._cache
        service = self._once("fix", build)
        if created:
            service.deps.operations = self.operations()
        return service

    def _local(self, worktree: Path, mode: str, report_dir: Path) -> LocalService:
        ext = self.externals
        probe = ext.port_probe or SocketProbe(float(self.config.get("localRun.portProbeTimeoutSeconds")))
        return LocalService(worktree, mode, report_dir, self.extensions(), self.config, self.conn, self.clock,
                            ext.spawner, probe, ext.process_table, self.transport, environ=dict(self.environ))

    def _verify_probes(self, target: ProbeTarget) -> dict[str, Any]:
        """合并前验证的接口浅跑与页面巡检：按本机服务的地址登录。"""
        return {ProbeKind.API_FUZZ.value: self.probe(ProbeKind.API_FUZZ, target.base_url), PAGES: self.pages()}

    def verify(self) -> VerifyService:
        def build() -> VerifyService:
            client = self.extensions()
            page_checks = client.configured(ExtensionPoint.PAGE_ROUTES) or any(self.layout.e2e_dir().glob("**/*.ts"))
            return VerifyService(VerifyDeps(
                self.layout, self.tool, self.config, self.conn, self.clock, self.events, self.runner(), self.git,
                self.tool_launcher(), executor=lambda target: self.regression_executor(target.base_url),
                local=self._local if client.configured(ExtensionPoint.LOCAL_RUN) else None,
                probes=self._verify_probes, extensions=client, operations=self.operations(),
                sync=self.sync_readonly, environ=dict(self.environ), zone=self.zone, page_checks=page_checks,
                revert=lambda issue_id, reason: self.release().revert(issue_id, reason)))

        return self._once("verify", build)

    def release(self) -> ReleaseService:
        created = "release" not in self._cache
        service = self._once("release", lambda: ReleaseService(ReleaseDeps(
            self.layout, self.config, self.conn, self.clock, self.events, self.git, planner=self.planner(),
            gh=self.gh, notifier=self.notifier, zone=self.zone, branch_prefix=self.user.branch_prefix,
            deploys=self.deploy_source)))
        if created:
            service.deps.operations = self.operations()
        return service

    def learn(self) -> LearnService:
        return self._once("learn", lambda: LearnService(LearnDeps(
            self.layout, self.tool, self.config, self.conn, self.clock, self.events, self.runner(), self.knowledge(),
            notifier=self.notifier, repo_query=self.repo_query, pid_alive=self.externals.alive, zone=self.zone,
            overrides=self.overrides, rules=self.rule_env())))

    def rule_env(self) -> RuleEnv:
        """缺陷变规则的验证：Semgrep 命令与超时同静态巡检，全仓库扫描用只读 worktree。"""
        settings = RuleCheckSettings(self.semgrep, float(self.config.get("runtime.sources.semgrepTimeoutSeconds")),
                                     self.config.whole_threshold("learn.rules.maxRepoHits"), dict(self.environ))
        return RuleEnv(self.git, self.tool_launcher(), settings, self.config.repo, self.layout.readonly_worktree())

    def improve(self) -> ImproveService:
        """自我改进的建议：评测经 eval 命令同一套依赖运行，用例取校验通过的用例(evals/ 中封存的用例，fix 另加
        data/eval/cases/ 中运行即评测的用例)。"""
        from tightrein.cli.commands.eval_ import dependencies

        def cases(stage: Stage) -> list[ModuleCase]:
            return list(verify_cases(self.layout, self.process, stage, rubric.item_ids).module_cases)

        return self._once("improve", lambda: ImproveService(ImproveDeps(
            self.layout, self.tool, self.config, self.conn, self.clock, self.events, self.runner(), cases,
            lambda plan: evaluation.evaluate(plan, dependencies(self)), zone=self.zone, overrides=self.overrides)))

    # 编排用的组合动作

    def repo_query(self, source: str) -> RepoFacts:
        """第三方 skill 核实的只读查询：`gh api repos/<所有者>/<仓库>`；失败时抛出 LookupError。"""
        return third_party.repo_facts(self.process, self.root, source)

    def run_command(self, argv: Sequence[str]) -> int:
        """以本工作区执行一条子命令(定时任务的 command)。cli.main 依赖本模块，因此在调用时导入。"""
        from tightrein.cli.main import dispatch

        return dispatch(self, argv)

    def purge(self) -> retention.RetentionReport:
        """保留期清理，先轮转 launchd 的日志。"""
        retention.rotate_launchd_logs(WorkspaceLayout(self.root),
                                      int(self.config.get("runtime.store.launchdLogMaxBytes")),
                                      int(self.config.get("runtime.store.launchdLogBackups")))
        return retention.purge(self.conn, WorkspaceLayout(self.root), self.clock, self.config.retention_policy())
