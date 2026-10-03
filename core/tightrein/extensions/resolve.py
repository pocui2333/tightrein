"""按 architecture/10 1.5 的五步查找每个扩展点由谁实现，并执行启动时的检查(1.6、4.2)。

查找顺序：
1. extensions.<扩展点>.enabled 为 false：核心默认，用于关闭某个扩展点；
2. 有 command：项目扩展，工作目录为 workspaces/<项目>/extensions/；mode 为 extend 时按第 4、5 步求出下一层
   (use 与 command 互斥，第 3 步不适用)，下一层是核心默认时报错，不降级为 replace；
3. 有 use：方法目录中的该方法；方法不存在、属于其他扩展点、所属技术栈没有在 stacks 中声明时报错；
4. stacks 中的技术栈在 stack.yaml 的 defaults 中为该扩展点指定了默认方法：该方法；多个技术栈都指定时报错，
   要求用 use 明确选择；
5. 以上都不满足：核心默认。

方法的实现：options 依次取技术栈方法清单中的默认值、config/defaults.yaml 与 project.yaml 的 methods.<方法编号>、
extensions.<扩展点>.options，后者覆盖前者的同名键(浅合并)，按方法的 optionsSchema 校验；
extend 模式下 options 同时传给两层，下一层只校验 optionsSchema 的 properties 中定义的键。技术栈方法的命令相对方法目录
解析，工作目录为技术栈目录，另加 stack.yaml 的 env，长期缓存目录按技术栈区分，version 为技术栈版本；核心方法以模块
运行，工作目录为工作区根目录，命令文件为模块文件(进入缓存键)。

命令中的 `{python}` 替换为核心虚拟环境的解释器。命令文件：第一项带路径时为第一项；否则第二项带扩展名或路径时
为第二项(由解释器或 PATH 中的程序运行的脚本)。命令文件相对扩展所在目录解析，必须存在，执行时换成绝对路径。
内部错误读取日志平台(sources.platform-errors.logQuery)而 log-parse 没有实现时报错。任何一项不满足都抛出 ConfigError，一次列出全部问题，每条带完整键名；
外部工具是否安装不在这里检查。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tightrein.config.project import ConfigError, ConfigIssue, ExtensionSetting, ProjectConfig
from tightrein.domain.enums import ExtensionLayer, ExtensionMode, ExtensionPoint
from tightrein.extensions import points
from tightrein.extensions.catalog import (
    CORE_SOURCE,
    MODULE_SUFFIX,
    STACKS_KEY,
    Catalog,
    Method,
    StackManifest,
    load_catalog,
    load_stack,
    option_problems,
)
from tightrein.extensions.points import PYTHON_PLACEHOLDER
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout


@dataclass(frozen=True)
class Implementation:
    """一个扩展点的实现。command 为配置或清单中的写法(进入缓存键与 ext list)，argv 为实际执行的参数；
    method 为选用的方法编号，项目扩展与核心默认为空。"""

    point: ExtensionPoint
    layer: ExtensionLayer
    timeout_seconds: int
    command: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()
    cwd: Path | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    cache_name: str | None = None
    version: str | None = None
    command_file: Path | None = None
    mode: ExtensionMode = ExtensionMode.REPLACE
    base: Implementation | None = None
    method: str | None = None


@dataclass(frozen=True)
class Resolution:
    implementations: Mapping[ExtensionPoint, Implementation]

    def get(self, point: ExtensionPoint) -> Implementation:
        return self.implementations[point]


def default(point: ExtensionPoint) -> Implementation:
    return Implementation(point, ExtensionLayer.DEFAULT, points.SPECS[point].timeout_seconds)


def _command_file_index(command: tuple[str, ...]) -> int | None:
    first = command[0]
    if first != PYTHON_PLACEHOLDER and "/" in first:
        return 0
    if len(command) > 1 and not command[1].startswith("-"):
        second = Path(command[1])
        if second.suffix or "/" in command[1]:
            return 1
    return None


def build_argv(
    command: tuple[str, ...], directory: Path, python: str,
) -> tuple[tuple[str, ...], Path | None, str | None]:
    """返回 (实际执行的参数, 命令文件, 问题)；命令文件不存在时问题不为空。"""
    argv = [python if part == PYTHON_PLACEHOLDER else part for part in command]
    index = _command_file_index(command)
    if index is None:
        return tuple(argv), None, None
    path = Path(command[index])
    target = path if path.is_absolute() else directory / path
    if not target.is_file():
        return tuple(argv), None, f"命令文件不存在：{target}"
    argv[index] = str(target)
    return tuple(argv), target, None


def _option_issues(method: Method, options: Mapping[str, Any], key: str, restrict: bool) -> list[ConfigIssue]:
    schema = method.options_schema
    if restrict:
        known = schema.get("properties", {})
        options = {name: value for name, value in options.items() if name in known}
    return [ConfigIssue(f"{key}.options", problem) for problem in option_problems(schema, options)]


def _stack_method(
    method: Method, stack: StackManifest, options: Mapping[str, Any], timeout: int, python: str,
) -> tuple[Implementation, list[ConfigIssue]]:
    argv, command_file, problem = build_argv(method.command, method.directory, python)
    issues: list[ConfigIssue] = []
    if problem is not None:
        issues.append(ConfigIssue(STACKS_KEY, f"方法 {method.id} 的 {method.point.value}：{problem}"))
    implementation = Implementation(
        method.point, ExtensionLayer.STACK, timeout, command=method.command, argv=argv, cwd=stack.root,
        options=options, env=stack.env, cache_name=stack.name, version=stack.version, command_file=command_file,
        method=method.id,
    )
    return implementation, issues


def method_implementation(
    method: Method, catalog: Catalog, config: ProjectConfig, workspace: WorkspaceLayout, setting: ExtensionSetting,
    timeout: int, python: str, *, restrict: bool = False,
) -> tuple[Implementation, list[ConfigIssue]]:
    """方法目录中某个方法的实现与 options 的问题。"""
    point = method.point
    options = {**method.options, **config.method_options(method.id), **setting.options}
    issues = _option_issues(method, options, f"extensions.{point.value}", restrict)
    if method.source != CORE_SOURCE:
        implementation, found = _stack_method(method, catalog.stacks[method.source], options, timeout, python)
        return implementation, issues + found
    argv, _, _ = build_argv(method.command, workspace.root, python)
    implementation = Implementation(
        point, ExtensionLayer.CORE, timeout, command=method.command, argv=argv, cwd=workspace.root, options=options,
        cache_name=CORE_SOURCE, command_file=method.manifest.with_suffix(MODULE_SUFFIX), method=method.id,
    )
    return implementation, issues


def _project_implementation(
    point: ExtensionPoint, setting: ExtensionSetting, workspace: WorkspaceLayout, timeout: int, python: str,
) -> tuple[Implementation, list[ConfigIssue]]:
    command = setting.command or ()
    directory = workspace.extensions_dir()
    argv, command_file, problem = build_argv(command, directory, python)
    issues = [] if problem is None else [ConfigIssue(f"extensions.{point.value}.command", problem)]
    implementation = Implementation(
        point, ExtensionLayer.PROJECT, timeout, command=command, argv=argv, cwd=directory,
        options=dict(setting.options), cache_name=workspace.project, command_file=command_file, mode=setting.mode,
    )
    return implementation, issues


def _chosen(point: ExtensionPoint, use: str, config: ProjectConfig, catalog: Catalog) -> tuple[Method | None, str]:
    """use 指向的方法，找不到时返回原因。"""
    source = use.split("/", 1)[0]
    if source != CORE_SOURCE and source not in config.stacks:
        return None, f"方法 {use} 所属的技术栈 {source} 没有在 stacks 中声明"
    method = catalog.find(point, use)
    if method is not None:
        return method, ""
    others = catalog.points_of(use)
    if others:
        return None, f"{use} 属于 {'、'.join(item.value for item in others)}，不能用于 {point.value}"
    return None, f"方法目录中没有 {use}"


def _stack_default(point: ExtensionPoint, config: ProjectConfig, catalog: Catalog) -> tuple[Method | None, str]:
    """stacks 中的技术栈为该扩展点指定的默认方法；多个技术栈都指定时返回原因。"""
    found = [(stack, catalog.stacks[stack].defaults[point]) for stack in config.stacks
             if stack in catalog.stacks and point in catalog.stacks[stack].defaults]
    if len(found) > 1:
        names = "、".join(stack for stack, _ in found)
        return None, f"技术栈 {names} 都为 {point.value} 指定了默认方法，用 use 明确选择"
    if not found:
        return None, ""
    stack, name = found[0]
    return catalog.find(point, f"{stack}/{name}"), ""


def _resolve_point(
    point: ExtensionPoint, config: ProjectConfig, catalog: Catalog, workspace: WorkspaceLayout, python: str,
) -> tuple[Implementation, list[ConfigIssue]]:
    setting = config.extension(point)
    if not setting.enabled:
        return default(point), []
    timeout = setting.timeout_seconds or points.SPECS[point].timeout_seconds
    key = f"extensions.{point.value}"
    if setting.command is not None:
        project, issues = _project_implementation(point, setting, workspace, timeout, python)
        if setting.mode is not ExtensionMode.EXTEND:
            return project, issues
        lower, reason = _stack_default(point, config, catalog)
        if lower is None:
            reason = reason or ("extend 需要下一层的实现，但没有声明 stacks" if not config.stacks
                                else f"extend 需要下一层的实现，stacks 中的技术栈都没有为 {point.value} 指定默认方法")
            return project, issues + [ConfigIssue(f"{key}.mode", reason)]
        base, found = method_implementation(lower, catalog, config, workspace, setting, timeout, python,
                                            restrict=True)
        return replace(project, base=base), issues + found
    if setting.use is not None:
        method, reason = _chosen(point, setting.use, config, catalog)
        if method is None:
            return default(point), [ConfigIssue(f"{key}.use", reason)]
        return method_implementation(method, catalog, config, workspace, setting, timeout, python)
    method, reason = _stack_default(point, config, catalog)
    if method is None:
        return default(point), [ConfigIssue(key, reason)] if reason else []
    return method_implementation(method, catalog, config, workspace, setting, timeout, python)


def resolve(
    config: ProjectConfig, workspace: WorkspaceLayout, tool: ToolLayout, *, python: str = sys.executable,
) -> Resolution:
    """启动时解析全部扩展点；不合格时抛出 ConfigError。"""
    catalog, issues = load_catalog(tool, config.stacks)
    implementations: dict[ExtensionPoint, Implementation] = {}
    for point in ExtensionPoint:
        implementations[point], found = _resolve_point(point, config, catalog, workspace, python)
        issues += found
    platform_errors = config.data.get("sources", {}).get("platform-errors", {})
    if platform_errors.get("logQuery") and implementations[ExtensionPoint.LOG_PARSE].layer is ExtensionLayer.DEFAULT:
        issues.append(ConfigIssue("extensions.log-parse",
                                  "sources.platform-errors.logQuery 有值时须选用 log-parse 方法(字段映射)"))
    if issues:
        raise ConfigError(config.path, issues)
    return Resolution(implementations)


def stack_method_implementations(tool: ToolLayout, stack: str, *, python: str = sys.executable) -> list[Implementation]:
    """技术栈中每个方法的实现，options 只取清单中的默认值；供 `ext test --stack` 以夹具运行。"""
    manifest, methods, issues = load_stack(tool, stack)
    implementations: list[Implementation] = []
    if manifest is not None:
        for method in methods:
            implementation, found = _stack_method(method, manifest, dict(method.options),
                                                  points.SPECS[method.point].timeout_seconds, python)
            implementations.append(implementation)
            issues += found
    if issues:
        raise ConfigError(tool.stack_manifest(stack), issues)
    return implementations


def project_implementations(
    config: ProjectConfig, workspace: WorkspaceLayout, *, python: str = sys.executable,
) -> dict[ExtensionPoint, Implementation]:
    """当前工作区声明了 command 且未关闭的项目扩展，不连接下一层；供 `ext test --workspace-extensions` 使用。"""
    implementations: dict[ExtensionPoint, Implementation] = {}
    issues: list[ConfigIssue] = []
    for point in ExtensionPoint:
        setting = config.extension(point)
        if not setting.enabled or setting.command is None:
            continue
        timeout = setting.timeout_seconds or points.SPECS[point].timeout_seconds
        implementations[point], found = _project_implementation(point, setting, workspace, timeout, python)
        issues += found
    if issues:
        raise ConfigError(config.path, issues)
    return implementations
