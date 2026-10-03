"""方法目录(architecture/10 1.4)：每个扩展点有哪些方法、每种方法的清单。

方法清单的字段见 extension/method-manifest.schema.json：名称、所属扩展点、一句话说明、适用条件、参数 schema(optionsSchema)、
参数的默认值与依赖的外部工具；方法编号为 `<来源>/<方法名>`，同一编号只在所属扩展点内有效(不同扩展点可以有同名方法)。

核心方法位于 `methods/<扩展点>/<方法>.py`，清单是同名的 `.yaml`；目录与文件名把扩展点名和方法名中的 `-` 写成 `_`，
以便作为模块运行：命令固定为 `{python} -m tightrein.extensions.methods.<扩展点>.<方法>`，清单中不写 command；
options 的默认值是可调的配置，写在 config/defaults.yaml 的 methods.<方法编号> 下，清单中不写 options。
清单不合格属于程序错误，抛出 ManifestError 并列出全部问题。

技术栈方法位于 `extensions/stacks/<技术栈>/<扩展点>/<方法>/method.yaml`，清单中必须写 command(相对方法目录解析)，
可以写 options 的默认值(技术栈自己的配置)。stack.yaml 给出技术栈的名称(须与目录名相同)、版本、外部工具、非敏感的
环境变量，以及 defaults：为某些扩展点指定的默认方法，必须是该技术栈中存在的方法。技术栈的问题记在 stacks 键下，
一次列出，由 resolve 以 ConfigError 报出。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from tightrein.config.project import ConfigIssue
from tightrein.contracts import validate
from tightrein.domain.enums import ExtensionLayer, ExtensionPoint
from tightrein.extensions.points import PYTHON_PLACEHOLDER, STACK_MANIFEST_SCHEMA
from tightrein.guards.credentials import sensitive_name
from tightrein.observability.redact import looks_like_credential
from tightrein.store.files import yaml_text
from tightrein.store.files.layout import ToolLayout

MANIFEST_SCHEMA = "extension/method-manifest.schema.json"
CORE_SOURCE = "core"
CORE_METHODS_DIR = Path(__file__).parent / "methods"
CORE_METHODS_PACKAGE = "tightrein.extensions.methods"
MANIFEST_SUFFIX = ".yaml"
MODULE_SUFFIX = ".py"
STACK_METHOD_FILE = "method.yaml"
STACKS_KEY = "stacks"


class ManifestError(ValueError):
    """方法清单不合格；problems 为每条问题(带文件路径)。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("方法清单不合格：\n" + "\n".join(self.problems))


@dataclass(frozen=True)
class ToolRequirement:
    tool: str
    min_version: str | None = None
    install: str | None = None


@dataclass(frozen=True)
class Method:
    """一种方法。directory 为方法所在目录；command 为清单中的命令(核心方法为模块运行命令)。"""

    source: str
    name: str
    point: ExtensionPoint
    summary: str
    applicability: str
    options: Mapping[str, Any]
    options_schema: Mapping[str, Any]
    tools: tuple[ToolRequirement, ...]
    command: tuple[str, ...]
    directory: Path
    manifest: Path
    doc: Mapping[str, Any] | None = None  # 方法参考的前提、输出、限制与示例配置

    @property
    def id(self) -> str:
        return f"{self.source}/{self.name}"

    @property
    def layer(self) -> ExtensionLayer:
        return ExtensionLayer.CORE if self.source == CORE_SOURCE else ExtensionLayer.STACK


def module_name(manifest: Path) -> str:
    """核心方法清单对应的模块名。"""
    return f"{CORE_METHODS_PACKAGE}.{manifest.parent.name}.{manifest.stem}"


def option_problems(schema: Mapping[str, Any], options: Mapping[str, Any]) -> list[str]:
    """options 不符合 schema 的每一处，写成「JSON 路径: 原因」。"""
    validator = Draft202012Validator(dict(schema))
    return [f"{error.json_path}: {error.message}"
            for error in sorted(validator.iter_errors(dict(options)), key=lambda error: error.json_path)]


def _checked(data: Any, manifest: Path, point: ExtensionPoint, name: str) -> list[str]:
    errors = validate.validate(MANIFEST_SCHEMA, data)
    if errors:
        return [f"{manifest}：{error}" for error in errors]
    problems = []
    if data["name"] != name:
        problems.append(f"{manifest}：name 为 {data['name']}，应为 {name}")
    if data["point"] != point.value:
        problems.append(f"{manifest}：point 为 {data['point']}，应为 {point.value}")
    schema = data["optionsSchema"]
    options = data.get("options", {})
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        problems.append(f"{manifest}：optionsSchema 不是合格的 JSON Schema：{error.message}")
    else:
        partial = {key: value for key, value in schema.items() if key != "required"}
        problems += [f"{manifest}：options 的默认值不合格：{problem}" for problem in option_problems(partial, options)]
    return problems


def parse(data: Any, manifest: Path, *, source: str, point: ExtensionPoint, name: str, directory: Path,
          command: tuple[str, ...], check: bool = True) -> tuple[Method | None, list[str]]:
    """按 schema 校验清单，并核对名称、扩展点、optionsSchema 与默认 options(必填项可以没有默认值)；返回 (方法, 问题)。
    check 为假时不校验，只用于已经校验过的清单(核心方法进程启动时读取自己的清单，省去加载全部 schema)。"""
    problems = _checked(data, manifest, point, name) if check else []
    if problems:
        return None, problems
    tools = tuple(ToolRequirement(item["tool"], item.get("minVersion"), item.get("install"))
                  for item in data.get("tools", []))
    method = Method(source, name, point, data["summary"], data["applicability"], data.get("options", {}),
                    data["optionsSchema"], tools, command, directory, manifest, data.get("doc"))
    return method, []


def _load_yaml(path: Path) -> tuple[Any, list[str]]:
    try:
        return yaml_text.load(path.read_text(encoding="utf-8")), []
    except (OSError, yaml_text.YamlError) as error:
        return None, [f"{path}：{error}"]


def read_core(manifest: Path, *, check: bool = True) -> Method:
    """读取一个核心方法的清单；不合格时抛出 ManifestError。check 的含义同 parse。"""
    points = {point.value.replace("-", "_"): point for point in ExtensionPoint}
    point = points.get(manifest.parent.name)
    if point is None:
        raise ManifestError([f"{manifest}：目录 {manifest.parent.name} 不是扩展点"])
    data, problems = _load_yaml(manifest)
    if not problems and isinstance(data, dict) and "command" in data:
        problems.append(f"{manifest}：核心方法以模块运行，清单中不写 command")
    if not problems and isinstance(data, dict) and "options" in data:
        problems.append(f"{manifest}：核心方法 options 的默认值写在 config/defaults.yaml，清单中不写 options")
    if not manifest.with_suffix(MODULE_SUFFIX).is_file():
        problems.append(f"{manifest}：找不到模块文件 {manifest.with_suffix(MODULE_SUFFIX).name}")
    if problems:
        raise ManifestError(problems)
    method, problems = parse(data, manifest, source=CORE_SOURCE, point=point, name=manifest.stem.replace("_", "-"),
                             directory=manifest.parent, command=(PYTHON_PLACEHOLDER, "-m", module_name(manifest)),
                             check=check)
    if method is None:
        raise ManifestError(problems)
    return method


def core_methods(root: Path = CORE_METHODS_DIR) -> tuple[Method, ...]:
    """全部核心方法，按扩展点的顺序、再按方法名排列；任一清单不合格时抛出 ManifestError，列出全部问题。"""
    methods: list[Method] = []
    problems: list[str] = []
    for manifest in sorted(root.glob(f"*/*{MANIFEST_SUFFIX}")):
        try:
            methods.append(read_core(manifest))
        except ManifestError as error:
            problems += error.problems
    if problems:
        raise ManifestError(problems)
    order = list(ExtensionPoint)
    return tuple(sorted(methods, key=lambda method: (order.index(method.point), method.name)))


@dataclass(frozen=True)
class StackManifest:
    name: str
    version: str
    root: Path
    env: Mapping[str, str]
    defaults: Mapping[ExtensionPoint, str]


@dataclass(frozen=True)
class Catalog:
    """核心方法与已加载的技术栈的方法。"""

    methods: tuple[Method, ...]
    stacks: Mapping[str, StackManifest]

    def for_point(self, point: ExtensionPoint) -> tuple[Method, ...]:
        return tuple(method for method in self.methods if method.point is point)

    def find(self, point: ExtensionPoint, method_id: str) -> Method | None:
        return next((method for method in self.for_point(point) if method.id == method_id), None)

    def points_of(self, method_id: str) -> tuple[ExtensionPoint, ...]:
        return tuple(method.point for method in self.methods if method.id == method_id)


def _stack_methods(tool: ToolLayout, stack: str) -> tuple[list[Method], list[str]]:
    methods: list[Method] = []
    problems: list[str] = []
    for point in ExtensionPoint:
        point_dir = tool.stack_dir(stack) / point.value
        if not point_dir.is_dir():
            continue
        for method_dir in sorted(path for path in point_dir.iterdir() if path.is_dir()):
            manifest = tool.method_manifest(stack, point, method_dir.name)
            if not manifest.is_file():
                problems.append(f"{method_dir} 中没有 {STACK_METHOD_FILE}")
                continue
            data, found = _load_yaml(manifest)
            if found:
                problems += found
                continue
            command = tuple(data.get("command", ())) if isinstance(data, dict) else ()
            method, found = parse(data, manifest, source=stack, point=point, name=method_dir.name,
                                  directory=method_dir, command=command)
            if method is not None and not command:
                found.append(f"{manifest}：技术栈方法须给出 command")
            problems += found
            if method is not None and command:
                methods.append(method)
    return methods, problems


def load_stack(tool: ToolLayout, stack: str) -> tuple[StackManifest | None, list[Method], list[ConfigIssue]]:
    """读取并校验 stack.yaml 与该技术栈的全部方法；不合格时清单为空，问题都记在 stacks 键下。"""
    path = tool.stack_manifest(stack)
    if not path.is_file():
        return None, [], [ConfigIssue(STACKS_KEY, f"找不到技术栈扩展的清单 {path}")]
    data, problems = _load_yaml(path)
    if problems:
        return None, [], [ConfigIssue(STACKS_KEY, problem) for problem in problems]
    errors = validate.validate(STACK_MANIFEST_SCHEMA, data)
    if errors:
        return None, [], [ConfigIssue(STACKS_KEY, f"{path} 不合格：{error}") for error in errors]
    issues = []
    if data["name"] != stack:
        issues.append(ConfigIssue(STACKS_KEY, f"{path} 的 name 为 {data['name']}，与目录名 {stack} 不一致"))
    env = dict(data.get("env", {}))
    for name, value in sorted(env.items()):
        if sensitive_name(name) or looks_like_credential(value):
            issues.append(ConfigIssue(STACKS_KEY, f"{path} 的 env 只能声明非敏感变量：{name}"))
    methods, problems = _stack_methods(tool, stack)
    issues += [ConfigIssue(STACKS_KEY, problem) for problem in problems]
    defaults = {ExtensionPoint(name): method for name, method in data.get("defaults", {}).items()}
    names = {(method.point, method.name) for method in methods}
    for point, method in defaults.items():
        if (point, method) not in names:
            issues.append(ConfigIssue(STACKS_KEY, f"{path} 的 defaults.{point.value} 为 {method}，"
                                                  f"{stack}/{point.value}/ 下没有这个方法"))
    manifest = StackManifest(data["name"], data["version"], tool.stack_dir(stack), env, defaults)
    return manifest, methods, issues


def installed_stacks(tool: ToolLayout) -> tuple[str, ...]:
    """本工具仓库中有 stack.yaml 的技术栈。"""
    root = tool.stacks_dir()
    if not root.is_dir():
        return ()
    return tuple(sorted(path.name for path in root.iterdir() if (path / "stack.yaml").is_file()))


def load_catalog(tool: ToolLayout, stacks: Iterable[str] | None = None,
                 core_root: Path = CORE_METHODS_DIR) -> tuple[Catalog, list[ConfigIssue]]:
    """核心方法加上 stacks 中的技术栈(为空时取全部已安装的技术栈)；返回 (方法目录, 技术栈的问题)。"""
    methods = list(core_methods(core_root))
    manifests: dict[str, StackManifest] = {}
    issues: list[ConfigIssue] = []
    for stack in installed_stacks(tool) if stacks is None else stacks:
        manifest, found, problems = load_stack(tool, stack)
        issues += problems
        if manifest is not None:
            manifests[stack] = manifest
            methods += found
    order = list(ExtensionPoint)
    methods.sort(key=lambda method: (order.index(method.point), method.source != CORE_SOURCE, method.source,
                                     method.name))
    return Catalog(tuple(methods), manifests), issues
