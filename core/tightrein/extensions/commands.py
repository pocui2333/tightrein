"""`tightrein ext list|methods|run|test` 背后的函数(architecture/10 1.4、第 8 章)；参数解析与输出格式由命令行
负责。

- list_points：每个扩展点的实现层、选用的方法、命令、生效的 options、超时与 extend 时的下一层。
- list_methods：方法目录中可选的方法(核心方法与本工具仓库中的全部技术栈)，供配置新项目时挑选。
- default_input 与 run_point：调用一次扩展点并返回经过校验的结果；不读写缓存，不更新读取位置。省略 `--input` 时按
  当前工作区组装输入：spec-export 的接口描述与 static-tools 的原始输出写到本次运行的 raw/ 下，authz-roles 取
  accounts.roles，error-tracking 与 log-platform 读最近 24 小时(log-platform 的查询取 sources.platform-errors.logQuery)，
  alert-source 没有输入，local-run 为 api 模式；log-parse 没有可用的缺省输入，必须给出 `--input`。
- discover_fixtures 与 run_fixtures：以项目扩展的 tests/fixtures/<扩展点>/<用例>/ 中的 input.json 为完整请求运行扩展，
  响应与 expected.json 逐项比对；两份文件中以 `{fixture}` 开头的字符串替换为用例目录，scratchDir 换成临时目录。
- run_stack_fixtures：技术栈的夹具多一层方法目录，tests/fixtures/<扩展点>/<方法>/<用例>/，由该方法的实现运行。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tightrein.config.layers import core_defaults
from tightrein.config.project import ConfigError, ProjectConfig
from tightrein.contracts import validate
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import ExtensionLayer, ExtensionMode, ExtensionPoint, Probe
from tightrein.extensions import points
from tightrein.extensions.catalog import ToolRequirement, load_catalog
from tightrein.extensions.client import MODE_API, checked, local_run_ports
from tightrein.extensions.invoke import (
    SCRATCH_PREFIX,
    Invoker,
    ProcessRequest,
    ProcessRunner,
    SubprocessRunner,
    extension_environment,
    parse_response,
)
from tightrein.extensions.resolve import Implementation, Resolution, stack_method_implementations
from tightrein.extensions.result import PointResult
from tightrein.store.files.layout import ToolLayout, UserLayout, WorkspaceLayout

FIXTURE_PLACEHOLDER = "{fixture}"
INPUT_FILE = "input.json"
EXPECTED_FILE = "expected.json"

TRIAL_HOURS = 24  # ext run 省略 --input 时平台读取的时间窗口
TRIAL_LIMIT = 200


@dataclass(frozen=True)
class PointListing:
    point: ExtensionPoint
    implementation: ExtensionLayer
    method: str | None
    command: tuple[str, ...]
    options: Mapping[str, Any]
    timeout_seconds: int
    mode: ExtensionMode
    base: ExtensionLayer | None


def list_points(resolution: Resolution) -> list[PointListing]:
    listings = []
    for point in ExtensionPoint:
        implementation = resolution.get(point)
        base = None if implementation.base is None else implementation.base.layer
        listings.append(PointListing(point, implementation.layer, implementation.method, implementation.command,
                                     dict(implementation.options), implementation.timeout_seconds,
                                     implementation.mode, base))
    return listings


def default_input(
    point: ExtensionPoint, config: ProjectConfig, layout: WorkspaceLayout, run_id: str,
) -> dict[str, Any]:
    """省略 `--input` 时的输入；log-parse 需要日志片段，没有缺省输入，抛出 ValueError。"""
    if point is ExtensionPoint.SPEC_EXPORT:
        return {"outputFile": str(layout.extension_trial_openapi(run_id).absolute())}
    if point is ExtensionPoint.AUTHZ_ROLES:
        return {"roles": list(config.roles())}
    if point in (ExtensionPoint.ERROR_TRACKING, ExtensionPoint.LOG_PLATFORM):
        until = datetime.now(timezone.utc).replace(microsecond=0)
        window = {"since": format_iso(until - timedelta(hours=TRIAL_HOURS)), "until": format_iso(until)}
        if point is ExtensionPoint.ERROR_TRACKING:
            return window
        query = config.data.get("sources", {}).get("platform-errors", {}).get("logQuery")
        if not query:
            raise ValueError("log-platform 需要查询语句：设置 sources.platform-errors.logQuery，或用 --input 给出 input")
        return {"query": query, **window, "limit": TRIAL_LIMIT}
    if point is ExtensionPoint.LOG_PARSE:
        raise ValueError("log-parse 需要日志片段，用 --input 给出 input 部分的 JSON")
    if point is ExtensionPoint.STATIC_TOOLS:
        raw_dir = layout.probe_raw_dir(run_id, Probe.STATIC)
        return {"level": "full", "baseCommit": None, "changedFiles": [], "rawDir": str(raw_dir.absolute())}
    if point is ExtensionPoint.LOCAL_RUN:
        return {"mode": MODE_API, "ports": local_run_ports(config, MODE_API)}
    if point is ExtensionPoint.DEPLOY_SOURCE:
        return {"branch": config.main_branch}
    return {}


def run_point(
    invoker: Invoker, resolution: Resolution, point: ExtensionPoint, input: Mapping[str, Any], *,
    repo: Path | None, commit: str | None,
) -> PointResult:
    """调用一次扩展点：不读写缓存，也不保存读取位置；输出在 schema 之外的检查与正式调用相同。"""
    if points.SPECS[point].uses_repo and (repo is None or commit is None):
        raise ValueError(f"{point.value} 需要仓库与 commit")
    for key in ("outputFile", "rawDir"):
        if key in input:
            target = Path(input[key])
            (target.parent if key == "outputFile" else target).mkdir(parents=True, exist_ok=True)
    result = invoker.call(resolution.get(point), repo=repo, commit=commit, input=input)
    return checked(result, input)


@dataclass(frozen=True)
class FixtureCase:
    """一个夹具用例；method 为技术栈夹具中的方法名，项目扩展的夹具为空。"""

    point: ExtensionPoint
    name: str
    directory: Path
    method: str | None = None


@dataclass(frozen=True)
class FixtureOutcome:
    case: FixtureCase
    passed: bool
    differences: tuple[str, ...]


def _subdirectories(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_dir())


def _cases(point: ExtensionPoint, directory: Path, method: str | None) -> list[FixtureCase]:
    return [FixtureCase(point, case_dir.name, case_dir, method) for case_dir in _subdirectories(directory)
            if (case_dir / INPUT_FILE).is_file() and (case_dir / EXPECTED_FILE).is_file()]


def discover_fixtures(fixtures_dir: Path, *, by_method: bool = False) -> list[FixtureCase]:
    """fixtures_dir/<扩展点>/<用例>/(by_method 时为 <扩展点>/<方法>/<用例>/)中同时有 input.json 与 expected.json 的
    用例；目录名不是扩展点时抛出 ValueError。"""
    if not fixtures_dir.is_dir():
        return []
    known = {point.value: point for point in ExtensionPoint}
    cases = []
    for point_dir in _subdirectories(fixtures_dir):
        if point_dir.name not in known:
            raise ValueError(f"{point_dir} 不是扩展点的名称")
        point = known[point_dir.name]
        if not by_method:
            cases += _cases(point, point_dir, None)
            continue
        for method_dir in _subdirectories(point_dir):
            cases += _cases(point, method_dir, method_dir.name)
    return cases


def substitute(value: Any, fixture_dir: Path) -> Any:
    """把以 `{fixture}` 开头的字符串换成用例目录下的绝对路径，递归处理对象与数组。"""
    if isinstance(value, str) and value.startswith(FIXTURE_PLACEHOLDER):
        rest = value[len(FIXTURE_PLACEHOLDER):].lstrip("/")
        return str(fixture_dir / rest) if rest else str(fixture_dir)
    if isinstance(value, Mapping):
        return {key: substitute(item, fixture_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, fixture_dir) for item in value]
    return value


def differences(expected: Any, actual: Any, path: str = "$") -> list[str]:
    """两份 JSON 的差异，每条为「JSON 路径: 期望 …，实际 …」。"""
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        found = []
        for key in sorted(set(expected) | set(actual)):
            if key not in actual:
                found.append(f"{path}.{key}: 期望存在，实际缺少")
            elif key not in expected:
                found.append(f"{path}.{key}: 期望没有，实际为 {json.dumps(actual[key], ensure_ascii=False)}")
            else:
                found += differences(expected[key], actual[key], f"{path}.{key}")
        return found
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        found = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            found += differences(left, right, f"{path}[{index}]")
        return found
    if expected == actual and type(expected) is type(actual):
        return []
    return [f"{path}: 期望 {json.dumps(expected, ensure_ascii=False)}，实际 {json.dumps(actual, ensure_ascii=False)}"]


def _load(path: Path, fixture_dir: Path) -> Any:
    return substitute(json.loads(path.read_text(encoding="utf-8")), fixture_dir)


def _run_case(
    case: FixtureCase, implementation: Implementation, runner: ProcessRunner, environ: Mapping[str, str],
    user: UserLayout, scratch_root: Path | None,
) -> FixtureOutcome:
    try:
        request = _load(case.directory / INPUT_FILE, case.directory)
        expected = _load(case.directory / EXPECTED_FILE, case.directory)
    except ValueError as error:
        return FixtureOutcome(case, False, (f"夹具不是合法的 JSON：{error}",))
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX, dir=scratch_root) as scratch:
        request = {**request, "scratchDir": scratch}
        outcome = runner(ProcessRequest(
            implementation.argv, implementation.cwd or case.directory,
            extension_environment(implementation, environ, user),
            json.dumps(request, ensure_ascii=False).encode("utf-8"), implementation.timeout_seconds,
        ))
    response = parse_response(outcome.stdout)
    if response is None:
        tail = outcome.stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:]
        reason = outcome.start_error or f"退出码 {outcome.exit_code}，标准输出不是单个 JSON 对象"
        return FixtureOutcome(case, False, (reason, *tail))
    found = [str(error) for error in validate.validate(points.RESPONSE_SCHEMA, response)]
    if not found and response["status"] == "ok":
        output_errors = validate.validate(points.SPECS[case.point].output_schema, response["output"])
        found += [f"$.output{error.path[1:]}: {error.reason}" for error in output_errors]
    found += differences(expected, response)
    return FixtureOutcome(case, not found, tuple(found))


def run_fixtures(
    implementations: Mapping[ExtensionPoint, Implementation], fixtures_dir: Path, user: UserLayout, *,
    runner: ProcessRunner | None = None, environ: Mapping[str, str] | None = None, scratch_root: Path | None = None,
) -> list[FixtureOutcome]:
    """逐个运行项目扩展的夹具；某个扩展点没有实现时，该扩展点的用例记为不通过。"""
    run = runner or SubprocessRunner()
    variables = dict(os.environ if environ is None else environ)
    outcomes = []
    for case in discover_fixtures(fixtures_dir):
        implementation = implementations.get(case.point)
        if implementation is None:
            outcomes.append(FixtureOutcome(case, False, (f"没有 {case.point.value} 的实现",)))
            continue
        outcomes.append(_run_case(case, implementation, run, variables, user, scratch_root))
    return outcomes


def run_stack_fixtures(
    tool: ToolLayout, stack: str, user: UserLayout, *, python: str = sys.executable,
    runner: ProcessRunner | None = None, environ: Mapping[str, str] | None = None, scratch_root: Path | None = None,
) -> list[FixtureOutcome]:
    """逐个运行技术栈方法的夹具；夹具目录对应的方法不存在时，该用例记为不通过。技术栈不合格时抛出 ConfigError。"""
    implementations = {(item.point, item.method): item
                       for item in stack_method_implementations(tool, stack, python=python)}
    run = runner or SubprocessRunner()
    variables = dict(os.environ if environ is None else environ)
    outcomes = []
    for case in discover_fixtures(tool.stack_fixtures_dir(stack), by_method=True):
        implementation = implementations.get((case.point, f"{stack}/{case.method}"))
        if implementation is None:
            reason = f"技术栈 {stack} 的 {case.point.value} 没有方法 {case.method}"
            outcomes.append(FixtureOutcome(case, False, (reason,)))
            continue
        outcomes.append(_run_case(case, implementation, run, variables, user, scratch_root))
    return outcomes


@dataclass(frozen=True)
class MethodListing:
    """`ext methods` 的一行；options 为默认值(核心方法取 config/defaults.yaml)，default_of 为把它设为该扩展点
    默认方法的技术栈。"""

    point: ExtensionPoint
    method: str
    layer: ExtensionLayer
    summary: str
    applicability: str
    tools: tuple[ToolRequirement, ...]
    options: Mapping[str, Any]
    options_schema: Mapping[str, Any]
    default_of: str | None


def list_methods(tool: ToolLayout, point: ExtensionPoint | None = None) -> list[MethodListing]:
    """全部可选的方法，按扩展点的顺序、核心方法在前；给出 point 时只列该扩展点。技术栈不合格时抛出 ConfigError。"""
    catalog, issues = load_catalog(tool)
    if issues:
        raise ConfigError(tool.stacks_dir(), issues)
    listings = []
    for method in catalog.methods:
        if point is not None and method.point is not point:
            continue
        stack = catalog.stacks.get(method.source)
        default_of = method.source if stack is not None and stack.defaults.get(method.point) == method.name else None
        options = {**method.options, **core_defaults().get("methods", {}).get(method.id, {})}
        listings.append(MethodListing(method.point, method.id, method.layer, method.summary, method.applicability,
                                      method.tools, options, dict(method.options_schema), default_of))
    return listings
