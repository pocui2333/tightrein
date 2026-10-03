"""配置的缺省层(architecture/01 5.1)：核心默认值 config/defaults.yaml 与技术栈默认值
extensions/stacks/<技术栈>/defaults.yaml 的读取、校验与合并，以及各层共用的键名与报错。

- 两层都按 config/project-config.schema.json 校验，不要求任何层级的必填项；技术栈层只能出现核心层已有的键；
- 技术栈层按 project.yaml 中 stacks 的顺序排列，后列的优先；两个技术栈给同一个键不同的值时，由工作区配置显式给出，
  否则报出键名(由 config.project 在读取 project.yaml 后检查)；
- 映射逐级展开为点分键名，列表与 thresholds 下的 {value, min, max} 可调项作为一个叶子；
- 列表键名后加 `+`(例如 `paths+`)表示追加，技术栈层的键检查按去掉 `+` 的键名，追加的键不算两个技术栈的冲突。
本模块只依赖 contracts 与 store.files，store、observability 等低层组件可以用 core_value 读取核心缺省值。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tightrein.contracts import validate
from tightrein.store.files import yaml_text

SCHEMA = "config/project-config.schema.json"
DEFAULTS_FILE = Path(__file__).with_name("defaults.yaml")
STACK_DEFAULTS = "defaults.yaml"
ROOT_KEY = "(顶层)"
MISSING_REASON = "缺少必填项"
UNKNOWN_REASON = "不认识的键"
TUNABLE_KEYS = frozenset({"value", "min", "max"})
CORE = "core"
STACK_PREFIX = "stack:"
APPEND = "+"                    # 列表键名后加 + 表示追加到下层的列表后面(architecture/01 5.1)


@dataclass(frozen=True, order=True)
class ConfigIssue:
    key: str
    reason: str

    def __str__(self) -> str:
        return f"{self.key}: {self.reason}"


class ConfigError(Exception):
    """配置文件校验失败；issues 按键名排序，列出全部问题。"""

    def __init__(self, path: Path, issues: Iterable[ConfigIssue]) -> None:
        self.path = path
        self.issues = tuple(sorted(issues))
        lines = "\n".join(str(issue) for issue in self.issues)
        super().__init__(f"{path} 校验失败：\n{lines}")


@dataclass(frozen=True)
class Layer:
    """一层缺省配置：name 为 core 或 stack:<名称>。"""

    name: str
    path: Path
    data: Mapping[str, Any]


def join_key(parts: Iterable[str | int]) -> str:
    """把 jsonschema 的路径拼成键名：映射的键以点分隔，数组下标写成 `[序号]`。"""
    key = ""
    for part in parts:
        if isinstance(part, int):
            key += f"[{part}]"
        else:
            key += f".{part}" if key else part
    return key


def child(parent: str, name: str) -> str:
    return f"{parent}.{name}" if parent else name


@lru_cache(maxsize=None)
def validator(schema: str = SCHEMA) -> Draft202012Validator:
    return Draft202012Validator(validate.inline(schema))


def schema_issues(error: ValidationError) -> Iterator[ConfigIssue]:
    """把一条 jsonschema 错误转成带完整键名的问题：缺少的必填项与不认识的键报到该键本身，其余报到出错的值。"""
    key = join_key(error.absolute_path)
    if error.validator == "required":
        for name in error.validator_value:
            if name not in error.instance:
                yield ConfigIssue(child(key, name), MISSING_REASON)
        return
    if error.validator == "additionalProperties" and error.validator_value is False:
        known = error.schema.get("properties", {})
        for name in error.instance:
            if name not in known:
                yield ConfigIssue(child(key, name), UNKNOWN_REASON)
        return
    yield ConfigIssue(key or ROOT_KEY, error.message)


def read_yaml(path: Path) -> Any:
    try:
        return yaml_text.load(path.read_text(encoding="utf-8"))
    except yaml_text.YamlError as error:
        raise ConfigError(path, [ConfigIssue(ROOT_KEY, str(error))]) from error


def load_defaults(path: Path) -> Mapping[str, Any]:
    """读取并校验一层缺省配置；不合格时抛出 ConfigError。任何层级的必填项都不要求，缺省配置只写有缺省值的键。"""
    data = read_yaml(path)
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(path, [ConfigIssue(ROOT_KEY, "顶层必须是映射")])
    errors = [error for error in validator().iter_errors(data) if error.validator != "required"]
    issues = sorted({issue for error in errors for issue in schema_issues(error)})
    if issues:
        raise ConfigError(path, issues)
    return data


@lru_cache(maxsize=1)
def core_defaults() -> Mapping[str, Any]:
    return load_defaults(DEFAULTS_FILE)


def lookup(data: Mapping[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise KeyError(key)
        node = node[part]
    return node


def core_value(key: str) -> Any:
    """核心缺省值中的一项；没有时抛出 KeyError。供没有工作区配置的调用取参数缺省值。"""
    return lookup(core_defaults(), key)


def merge(lower: Mapping[str, Any], upper: Mapping[str, Any]) -> dict[str, Any]:
    """按 5.1 的合并规则：映射按键递归合并，标量与列表由上层整体替换。"""
    found = dict(lower)
    for name, value in upper.items():
        below = found.get(name)
        if isinstance(value, Mapping) and isinstance(below, Mapping):
            found[name] = merge(below, value)
        else:
            found[name] = value
    return found


def flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, value in data.items():
        key = f"{prefix}.{name}" if prefix else str(name)
        if isinstance(value, Mapping) and value and set(value) != TUNABLE_KEYS:
            found.update(flatten(value, key))
        else:
            found[key] = value
    return found


def _known(key: str, core_keys: set[str]) -> bool:
    """键在核心层中存在：是某个叶子，或是某个叶子的上级(核心层中为空映射的键也算)。"""
    return key in core_keys or any(name.startswith(f"{key}.") or key.startswith(f"{name}.") for name in core_keys)


def stack_layers(stacks_dir: Path, stacks: Sequence[str]) -> tuple[Layer, ...]:
    """各技术栈的缺省层，按 stacks 的顺序；没有 defaults.yaml 的技术栈不产生层。"""
    core_keys = set(flatten(core_defaults()))
    layers = []
    for stack in stacks:
        path = stacks_dir / stack / STACK_DEFAULTS
        if not path.is_file():
            continue
        data = load_defaults(path)
        unknown = [key for key in flatten(data) if not _known(key.removesuffix(APPEND), core_keys)]
        if unknown:
            raise ConfigError(path, [ConfigIssue(key, "核心缺省值中没有这个键") for key in unknown])
        layers.append(Layer(f"{STACK_PREFIX}{stack}", path, data))
    return tuple(layers)


def stack_conflicts(layers: Sequence[Layer], project: Mapping[str, Any]) -> list[ConfigIssue]:
    """两个技术栈给同一个键不同的值、工作区配置又没有给出这个键时的问题。"""
    seen: dict[str, tuple[str, Any]] = {}
    issues = []
    project_keys = set(flatten(project))
    for layer in layers:
        for key, value in flatten(layer.data).items():
            if key.endswith(APPEND):
                continue  # 追加的列表依次拼接，不冲突
            if key in seen and seen[key][1] != value and not _known(key, project_keys):
                issues.append(ConfigIssue(key, f"{seen[key][0]} 与 {layer.name} 的取值不同，须在 project.yaml 中给出"))
            seen.setdefault(key, (layer.name, value))
    return issues
