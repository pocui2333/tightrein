"""供采集方法与 verify 调用的类型化接口(architecture/10 1.3、2.7 与第 3 章)：每个扩展点一个函数，都返回 PointResult。

- 按 commit 缓存的扩展点(spec-export、authz-endpoints、authz-roles、page-routes)先读缓存；缓存缺失时要求 repo 的
  HEAD 就是所需的 commit，否则不调用扩展，抛出 WorktreeNotAtCommit，由调用方按「只读 worktree 不在目标 commit」处理。
  实现不读仓库时(needs_repo 为假：core/openapi-file 以 base: workspace 读取工作区中的接口描述)不要求 worktree，
  请求的 repo 为空，仍按 commit 缓存。
  命中缓存时同样写一个 run_script span，cached 为 true。
- spec-export 把接口描述写到 data/specs/<commit>/openapi.json；收到响应后核对 specFile 等于 outputFile、文件是可解析的
  JSON 且含 paths，否则按 schema-invalid 处理；成功时 logFile 复制到 raw/api-fuzz/spec-export.log。
- local-run 输出的 services[].env 只能是非敏感值，名称或值像凭证时按 schema-invalid 处理。
- deploy-source 的输入为 project.mainBranch，方法返回最近的部署记录，按合并提交匹配由 pipeline/common/deploys.py 完成。
- error-tracking、log-platform 按调用方给出的时间窗口读取，读取位置由调用方随信号在同一事务中保存；alert-source 没有输入。
- local-run 的端口取 localRun.ports：api 模式只有后端(localRun.ports.api)，page 模式后端为 backendForPages、
  前端为 frontend(architecture/07 11.2)。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint, ProbeLevel
from tightrein.extensions import defaults
from tightrein.extensions.cache import ExtensionCache
from tightrein.extensions.invoke import Invoker
from tightrein.extensions.methods.spec_export.openapi_file import BASE_WORKSPACE
from tightrein.extensions.resolve import Implementation, Resolution
from tightrein.extensions.result import ExtensionFailure, PointResult
from tightrein.guards.credentials import sensitive_name
from tightrein.observability.redact import looks_like_credential

MODE_API = "api"
MODE_PAGE = "page"
OPENAPI_FILE = "core/openapi-file"

HeadReader = Callable[[Path], str | None]


class WorktreeNotAtCommit(Exception):
    """缓存中没有该 commit 的结果，而 repo 为空或 repo 的 HEAD 不在这个 commit。"""

    def __init__(self, repo: Path | None, commit: str, head: str | None) -> None:
        self.repo = repo
        self.commit = commit
        self.head = head
        where = "没有只读 worktree" if repo is None else f"{repo} 的 HEAD 为 {head or '空'}"
        super().__init__(f"{where}，不在 {commit}，缓存中也没有该 commit 的结果")


@dataclass(frozen=True)
class StaticScope:
    level: ProbeLevel
    base_commit: str | None
    changed_files: tuple[str, ...] = ()


def _port(config: ProjectConfig, key: str) -> int | None:
    try:
        return config.get(key)
    except MissingSetting:
        return None


def local_run_ports(config: ProjectConfig, mode: str) -> dict[str, int | None]:
    """端口按模式取自 localRun.ports；没有配置的为空，项目没有 local-run 扩展时用不到端口。"""
    if mode == MODE_API:
        return {"backend": _port(config, "localRun.ports.api"), "frontend": None}
    if mode == MODE_PAGE:
        return {
            "backend": _port(config, "localRun.ports.backendForPages"),
            "frontend": _port(config, "localRun.ports.frontend"),
        }
    raise ValueError(f"local-run 的模式只能是 {MODE_API} 或 {MODE_PAGE}：{mode}")


def _spec_problem(input: Mapping[str, Any], output: Mapping[str, Any]) -> str | None:
    spec_file = output["specFile"]
    if spec_file != input["outputFile"]:
        return f"specFile 为 {spec_file}，应等于 outputFile {input['outputFile']}"
    try:
        document = json.loads(Path(spec_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return f"接口描述 {spec_file} 无法读取为 JSON：{type(error).__name__}"
    if not isinstance(document, dict) or "paths" not in document:
        return f"接口描述 {spec_file} 中没有 paths"
    return None


def _local_run_problem(output: Mapping[str, Any]) -> str | None:
    for service in output["services"]:
        for name, value in sorted(service["env"].items()):
            if sensitive_name(name) or looks_like_credential(value):
                return f"服务 {service['name']} 的环境变量 {name} 疑似凭证，local-run 只能给出非敏感变量"
    return None


def check_output(point: ExtensionPoint, input: Mapping[str, Any], output: Mapping[str, Any]) -> str | None:
    """schema 之外由核心检查的项(architecture/10 3.1、3.8)；不满足时返回原因。"""
    if point is ExtensionPoint.SPEC_EXPORT:
        return _spec_problem(input, output)
    if point is ExtensionPoint.LOCAL_RUN:
        return _local_run_problem(output)
    return None


def checked(result: PointResult, input: Mapping[str, Any]) -> PointResult:
    if result.output is None:
        return result
    problem = check_output(result.point, input, result.output)
    if problem is None:
        return result
    failure = ExtensionFailure(ExtensionErrorCode.SCHEMA_INVALID, problem)
    return PointResult(result.point, result.implementation, failure=failure, notes=result.notes)


class ExtensionClient:
    def __init__(
        self, config: ProjectConfig, resolution: Resolution, invoker: Invoker, cache: ExtensionCache, *,
        head: HeadReader,
    ) -> None:
        self.config = config
        self.resolution = resolution
        self.invoker = invoker
        self.cache = cache
        self.head = head

    def configured(self, point: ExtensionPoint) -> bool:
        """该扩展点由技术栈或项目实现(不是核心的缺省「未配置」)。"""
        return self.resolution.get(point).layer is not ExtensionLayer.DEFAULT

    def method(self, point: ExtensionPoint) -> str | None:
        """该扩展点选用的方法编号；不是方法目录中的方法(或没有实现)时为空。"""
        return self.resolution.get(point).method

    def needs_repo(self, point: ExtensionPoint) -> bool:
        """该扩展点的实现是否要读取目标 commit 的只读 worktree。"""
        implementation = self.resolution.get(point)
        return not (implementation.method == OPENAPI_FILE and implementation.options.get("base") == BASE_WORKSPACE)

    def spec_export(self, repo: Path | None, commit: str) -> PointResult:
        output_file = self.invoker.layout.openapi(commit).absolute()
        return self._cached(ExtensionPoint.SPEC_EXPORT, repo, commit, {"outputFile": str(output_file)})

    def authz_endpoints(self, repo: Path, commit: str) -> PointResult:
        return self._cached(ExtensionPoint.AUTHZ_ENDPOINTS, repo, commit, {})

    def authz_roles(self, repo: Path, commit: str, roles: Sequence[str]) -> PointResult:
        return self._cached(ExtensionPoint.AUTHZ_ROLES, repo, commit, {"roles": list(roles)})

    def page_routes(self, repo: Path, commit: str) -> PointResult:
        return self._cached(ExtensionPoint.PAGE_ROUTES, repo, commit, {})

    def error_tracking(self, since: datetime, until: datetime) -> PointResult:
        return self._platform(ExtensionPoint.ERROR_TRACKING, {"since": format_iso(since), "until": format_iso(until)})

    def log_platform(self, query: str, since: datetime, until: datetime, limit: int) -> PointResult:
        return self._platform(ExtensionPoint.LOG_PLATFORM, {"query": query, "since": format_iso(since),
                                                            "until": format_iso(until), "limit": limit})

    def alert_source(self) -> PointResult:
        return self._platform(ExtensionPoint.ALERT_SOURCE, {})

    def _platform(self, point: ExtensionPoint, input: Mapping[str, Any]) -> PointResult:
        implementation = self.resolution.get(point)
        if implementation.layer is ExtensionLayer.DEFAULT:
            return defaults.result(point)
        return self.invoker.call(implementation, repo=None, commit=None, input=input)

    def deploy_source(self, repo: Path, commit: str) -> PointResult:
        """最近的部署记录；commit 为仓库当前的头部(请求只用来定位仓库)。没有配置部署来源时为核心默认结果。"""
        implementation = self.resolution.get(ExtensionPoint.DEPLOY_SOURCE)
        if implementation.layer is ExtensionLayer.DEFAULT:
            return defaults.result(ExtensionPoint.DEPLOY_SOURCE)
        return self.invoker.call(implementation, repo=repo, commit=commit,
                                 input={"branch": self.config.main_branch})

    def log_parse(self, chunks: Sequence[Mapping[str, Any]], state: Mapping[str, Any] | None) -> PointResult:
        input = {"chunks": [dict(chunk) for chunk in chunks], "state": None if state is None else dict(state)}
        return self.invoker.call(self.resolution.get(ExtensionPoint.LOG_PARSE), repo=None, commit=None, input=input)

    def static_tools(self, repo: Path, commit: str, scope: StaticScope, raw_dir: Path) -> PointResult:
        input = {
            "level": scope.level.value,
            "baseCommit": scope.base_commit,
            "changedFiles": list(scope.changed_files),
            "rawDir": str(raw_dir.absolute()),
        }
        implementation = self.resolution.get(ExtensionPoint.STATIC_TOOLS)
        if implementation.layer is not ExtensionLayer.DEFAULT:
            raw_dir.mkdir(parents=True, exist_ok=True)
        return self.invoker.call(implementation, repo=repo, commit=commit, input=input)

    def local_run(self, worktree: Path, mode: str, ports: Mapping[str, int | None]) -> PointResult:
        implementation = self.resolution.get(ExtensionPoint.LOCAL_RUN)
        if implementation.layer is ExtensionLayer.DEFAULT:
            return defaults.result(ExtensionPoint.LOCAL_RUN)
        if ports.get("backend") is None:
            key = "localRun.ports.api" if mode == MODE_API else "localRun.ports.backendForPages"
            raise MissingSetting(key)
        commit = self.head(worktree)
        if commit is None:
            raise ValueError(f"{worktree} 没有 HEAD，无法生成启动计划")
        input = {"mode": mode, "ports": dict(ports)}
        return checked(self.invoker.call(implementation, repo=worktree, commit=commit, input=input), input)

    def _cached(self, point: ExtensionPoint, repo: Path | None, commit: str, input: Mapping[str, Any]) -> PointResult:
        implementation = self.resolution.get(point)
        if implementation.layer is ExtensionLayer.DEFAULT:
            return defaults.result(point)
        hit = self.cache.read(commit, implementation)
        if hit is not None and check_output(point, input, hit) is None:
            self._record_hit(implementation)
            return PointResult(point, implementation.layer, output=hit, cached=True)
        if self.needs_repo(point):
            head = self.head(repo) if repo is not None else None
            if repo is None or head is None or not head.startswith(commit):
                raise WorktreeNotAtCommit(repo, commit, head)
        else:
            repo = None
        if point is ExtensionPoint.SPEC_EXPORT:
            Path(input["outputFile"]).parent.mkdir(parents=True, exist_ok=True)
        result = checked(self.invoker.call(implementation, repo=repo, commit=commit, input=input), input)
        if result.output is not None:
            self.cache.write(commit, implementation, result.output)
            if point is ExtensionPoint.SPEC_EXPORT:
                self._keep_spec_log(result.output)
        return result

    def _keep_spec_log(self, output: Mapping[str, Any]) -> None:
        log_file = output["logFile"]
        if log_file is None or not Path(log_file).is_file():
            return
        target = self.invoker.layout.spec_export_log(self.invoker.run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log_file, target)

    def _record_hit(self, implementation: Implementation) -> None:
        tracer = self.invoker.tracer
        if tracer is None:
            return
        with tracer.span("run_script", attributes={
            "point": implementation.point.value, "implementation": implementation.layer.value,
            "command": self.invoker.redactor.text(" ".join(implementation.command)), "exitCode": None,
            "status": "ok", "errorCode": None, "durationMs": 0, "cached": True,
        }):
            pass
