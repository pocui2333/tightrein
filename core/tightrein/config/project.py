"""读取与校验 project.yaml(architecture/01 5.2)。

校验一次列出全部问题，每条带完整键名，例如 `accounts.login.tokenPath`：
1. 按 config/project-config.schema.json 校验结构：缺少的必填项与不认识的键报到该键本身，其余报到出错的值；
2. thresholds 中每个 `{value, min, max}` 满足 min ≤ value ≤ max；各层合并后 thresholds.autonomy.planMaxFiles、planMaxLines
   不大于 thresholds.change.maxFiles、maxLines；
3. 模型别名与路由表(config.routes)：路由键是登记的调用点及其条件，引用的别名存在，同一模型在各别名中价格一致，
   证伪复核与深度评审按解析后的实际工具与模型与生成者不同；旧的选模型键先报出(config.routes.LEGACY_HINT)，不做结构校验；
4. extensions.<扩展点> 的 use 与 command 不同时出现。
结构校验用展开引用后的 schema 直接取得 jsonschema 的结构化错误，从中得到缺少的键名，不解析错误文字。

缺省值来自核心 config/defaults.yaml 与 stacks 所列技术栈的 defaults.yaml(config.layers)；get、tunable 与
method_options 依次取 project.yaml、用户路由层、技术栈层(后列的技术栈优先)与核心层。用户路由层是本机用户配置顶层的
models 与 routes(config.user)，是个人缺省，排在 project.yaml 之下：两段按别名、按路由键合并，project.yaml 的同一项
整体替换。核心不给别名与路由，都没有时在运行时报出调用点。

stacks、extensions 与 methods 段在这里只做结构校验并提供读取；扩展点由哪个实现提供、选用的方法是否存在、
stack.yaml 与方法的 options 是否合格，由能力层的 extensions 在启动时解析(architecture/10 1.4 到 1.6、4.2)。
缺省值只登记文档给出的通用值，项目专属的取值(例如本机启动的端口)不设缺省值，未配置时报出完整键名。
顶层只有 project 必填；target 与 accounts 可以省略，省略时 base_url 为空、roles 为空，需要地址的步骤各自记为未配置
或 skipped，api-fuzz 与页面检查在没有角色时以匿名身份运行；部署来源由 extensions.deploy-source 选用，没有时以合并时间
加观察期为准。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from tightrein.config import layers
from tightrein.config import routes as model_routes
from tightrein.config.routes import ModelChoice, Routes
from tightrein.config.layers import (
    APPEND,
    MISSING_REASON,
    ROOT_KEY,
    TUNABLE_KEYS,
    UNKNOWN_REASON,
    ConfigError,
    ConfigIssue,
    Layer,
)
from tightrein.domain.enums import ExtensionMode, ExtensionPoint, LogLevel
from tightrein.store.files.layout import ToolLayout
from tightrein.store.retention import RetentionPolicy

__all__ = ["MISSING_REASON", "ROOT_KEY", "UNKNOWN_REASON", "ConfigError", "ConfigIssue"]

STACK_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

AUTONOMY_SIZE_GATES = (("autonomy.planMaxFiles", "change.maxFiles"), ("autonomy.planMaxLines", "change.maxLines"))


class MissingSetting(KeyError):
    """配置中没有该键，也没有缺省值。"""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"project.yaml 中没有 {key}，且没有缺省值")


@dataclass(frozen=True)
class ExtensionSetting:
    """extensions.<扩展点> 的一项；project.yaml 中没有这一项时各字段取缺省值。"""

    use: str | None = None
    command: tuple[str, ...] | None = None
    mode: ExtensionMode = ExtensionMode.REPLACE
    options: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: int | None = None
    enabled: bool = True


def _tunables(node: Any, key: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if not isinstance(node, Mapping):
        return
    if set(node) == TUNABLE_KEYS:
        yield key, node
        return
    for name, item in node.items():
        yield from _tunables(item, layers.child(key, name))


def _threshold_issues(thresholds: Mapping[str, Any]) -> Iterator[ConfigIssue]:
    for key, item in _tunables(thresholds, "thresholds"):
        value, low, high = item["value"], item["min"], item["max"]
        if low > high:
            yield ConfigIssue(key, f"min {low} 大于 max {high}")
        elif not low <= value <= high:
            yield ConfigIssue(key, f"value {value} 越出 [{low}, {high}]")


def _first(sources: Sequence[Mapping[str, Any]], key: str) -> Any:
    """第一个给出该键的层中的值；核心层对 check 用到的键都有缺省值。"""
    for source in sources:
        try:
            return layers.lookup(source, key)
        except KeyError:
            pass
    raise MissingSetting(key)


def _size_issues(sources: Sequence[Mapping[str, Any]]) -> Iterator[ConfigIssue]:
    """自动确认计划的改动量门槛不得大于硬上限(各层合并后比较)。"""
    for gate, cap in AUTONOMY_SIZE_GATES:
        try:
            limit = _first(sources, f"thresholds.{cap}")["value"]
            value = _first(sources, f"thresholds.{gate}")["value"]
        except MissingSetting:
            continue
        if value > limit:
            yield ConfigIssue(f"thresholds.{gate}", f"value {value} 大于 thresholds.{cap} 的 {limit}")


def _extension_issues(extensions: Mapping[str, Any]) -> Iterator[ConfigIssue]:
    for point, item in extensions.items():
        if "use" in item and "command" in item:
            yield ConfigIssue(f"extensions.{point}.use", "use 与 command 只能二选一")


def check(data: Any, defaults: Sequence[Mapping[str, Any]] = ()) -> list[ConfigIssue]:
    """返回全部问题；结构不合格时不再做后两步，避免在缺键的数据上误报。defaults 为按优先次序排列的缺省层
    (用户 agent 层、技术栈层、核心层)，缺省时只有核心层。"""
    if not isinstance(data, Mapping):
        return [ConfigIssue(ROOT_KEY, "顶层必须是映射")]
    issues = model_routes.legacy_issues(data) or \
        sorted({issue for error in layers.validator().iter_errors(data) for issue in layers.schema_issues(error)})
    if issues:
        return issues
    sources = [data, *(defaults or [layers.core_defaults()])]
    return sorted([*_threshold_issues(data.get("thresholds", {})),
                   *_size_issues(sources),
                   *model_routes.issues(sources),
                   *_extension_issues(data.get("extensions", {}))])


def core_config() -> ProjectConfig:
    """只含核心缺省值的配置，供不需要工作区的命令(install、third-party、skills check)读取可调项。"""
    return ProjectConfig(layers.DEFAULTS_FILE, {}, Routes())


@dataclass(frozen=True)
class ProjectConfig:
    """校验通过的 project.yaml。data 为读出的原始映射，routing 为本机用户配置顶层的 models 与 routes，调用方只读不改。"""

    path: Path
    data: Mapping[str, Any]
    routes: Routes
    stack_layers: tuple[Layer, ...] = ()
    routing: Mapping[str, Any] = field(default_factory=dict)

    def sources(self) -> list[Mapping[str, Any]]:
        """按优先次序排列的各层：project.yaml、用户路由层、技术栈层(后列的优先)、核心层。"""
        return [self.data, self.routing, *(layer.data for layer in reversed(self.stack_layers)),
                layers.core_defaults()]

    @property
    def name(self) -> str:
        return self.data["project"]["name"]

    @property
    def language(self) -> str:
        """给人读的文字的语言代码(project.language，缺省 en)。"""
        return str(self.get("project.language"))

    @property
    def repo(self) -> Path:
        return Path(self.data["project"]["repo"])

    @property
    def main_branch(self) -> str:
        return self.data["project"]["mainBranch"]

    @property
    def stacks(self) -> tuple[str, ...]:
        """用到的技术栈；省略时为空。"""
        return tuple(self.data.get("stacks", ()))

    def extension(self, point: ExtensionPoint) -> ExtensionSetting:
        item = self.data.get("extensions", {}).get(point.value)
        if item is None:
            return ExtensionSetting()
        command = item.get("command")
        return ExtensionSetting(
            use=item.get("use"),
            command=None if command is None else tuple(command),
            mode=ExtensionMode(item.get("mode", ExtensionMode.REPLACE.value)),
            options=copy.deepcopy(item.get("options", {})),
            timeout_seconds=item.get("timeoutSeconds"),
            enabled=item.get("enabled", True),
        )

    def method_options(self, method: str) -> dict[str, Any]:
        """核心方法 options 的默认值：各层 methods.<方法编号> 的同名项，上层覆盖下层。"""
        found: dict[str, Any] = {}
        for source in reversed(self.sources()):
            found.update(copy.deepcopy(source.get("methods", {}).get(method, {})))
        return found

    def error_levels(self) -> tuple[LogLevel, ...]:
        """内部错误从日志平台取的条目中产出信号的归一化级别，缺省为 error、critical。"""
        return tuple(LogLevel(value) for value in self.get("sources.platform-errors.levels"))

    def get(self, key: str) -> Any:
        """按点分隔的完整键名取值，依次取 project.yaml、用户 agent 层、技术栈层与核心层；都没有时抛出 MissingSetting。
        列表可在上层以 `<键>+` 追加(architecture/01 5.1)：取到某层的值后，依次拼上该层与更上层 `<键>+` 的列表。"""
        extras: list[Any] = []
        for source in self.sources():
            try:
                extras.append(layers.lookup(source, key + APPEND))
            except KeyError:
                pass
            try:
                value = layers.lookup(source, key)
            except KeyError:
                continue
            if not extras or not isinstance(value, list):
                return value
            return [*value, *(item for extra in reversed(extras) for item in extra)]
        if extras:
            return [item for extra in reversed(extras) for item in extra]
        raise MissingSetting(key)

    def appended(self, key: str) -> tuple[Any, ...]:
        """只允许追加的列表(例如 credentialFiles)：核心层、技术栈层与 project.yaml 中的值依次拼接，去掉重复项。"""
        found: list[Any] = []
        for source in reversed(self.sources()):
            try:
                found += layers.lookup(source, key)
            except KeyError:
                pass
        return tuple(dict.fromkeys(found))

    def non_working_days(self) -> frozenset[date]:
        return frozenset(date.fromisoformat(day) for day in self.data.get("schedule", {}).get("nonWorkingDays", ()))

    def tunable(self, key: str) -> float | None:
        """thresholds 下可调项的 value(键名不带 thresholds 前缀)，按 get 的层次查找；都没有时返回 None。"""
        item = None
        for source in self.sources():
            try:
                item = layers.lookup(source, f"thresholds.{key}")
                break
            except KeyError:
                pass
        if item is None:
            return None
        if not isinstance(item, Mapping) or set(item) != TUNABLE_KEYS:
            raise MissingSetting(f"thresholds.{key}")
        return item["value"]

    def threshold(self, key: str) -> float:
        """thresholds 下某项的 value；各层都没有时抛出 MissingSetting。"""
        value = self.tunable(key)
        if value is None:
            raise MissingSetting(f"thresholds.{key}")
        return value

    def whole_threshold(self, key: str) -> int:
        """次数、天数等只能取整数的阈值。"""
        value = self.threshold(key)
        if float(value) != int(value):
            raise ConfigError(self.path, [ConfigIssue(f"thresholds.{key}", f"须为整数：{value}")])
        return int(value)

    def retention_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            signals_days=self.whole_threshold("retention.signalsDays"),
            raw_days=self.whole_threshold("retention.rawDays"),
            logs_days=self.whole_threshold("retention.logsDays"),
            fixes_days=self.whole_threshold("retention.fixesDays"),
        )

    @property
    def base_url(self) -> str | None:
        """target.baseUrl；没有配置 target 时为空。"""
        return self.data.get("target", {}).get("baseUrl")


    @property
    def issue_tracker(self) -> str:
        """Issue 的去向(issues.tracker)：local 只在本地，github 时另有 GitHub 镜像。"""
        return str(self.get("issues.tracker"))

    def roles(self) -> tuple[str, ...]:
        """accounts.roles 中的角色；没有配置 accounts 时为空。"""
        return tuple(self.data.get("accounts", {}).get("roles", {}))

    def keychain_item(self, role: str) -> str:
        roles = self.data.get("accounts", {}).get("roles", {})
        if role not in roles:
            raise MissingSetting(f"accounts.roles.{role}")
        return roles[role]["keychain"]

    def model_choice(self, point: str, conditions: Sequence[str] = (), *, tool: str | None = None,
                     model: str | None = None) -> ModelChoice:
        """调用点(config.routes.CALL_POINTS，或 default)按路由表解析的工具与模型，tool、model 为命令行的改写；
        没有路由时抛出 RouteError。"""
        return self.routes.choose(point, conditions, tool=tool, model=model)


def parse(data: Any, path: Path, tool_root: Path | None = None,
          routing: Mapping[str, Any] | None = None) -> ProjectConfig:
    """校验 project.yaml 的内容并读取 stacks 所列技术栈的缺省层；tool_root 缺省为核心所在仓库，routing 为本机用户配置
    顶层的 models 与 routes。"""
    stacks = data.get("stacks", ()) if isinstance(data, Mapping) else ()
    names = [name for name in stacks if isinstance(name, str) and STACK_NAME.fullmatch(name)] \
        if isinstance(stacks, list) else []
    tool = ToolLayout(tool_root) if tool_root is not None else ToolLayout()
    found = layers.stack_layers(tool.stacks_dir(), names)
    routing = routing or {}
    defaults = [routing, *(layer.data for layer in reversed(found)), layers.core_defaults()]
    issues = check(data, defaults)
    if not issues:
        issues = layers.stack_conflicts(found, data)
    if issues:
        raise ConfigError(path, issues)
    sources = [data, *defaults]
    return ProjectConfig(path, data, Routes(model_routes.merged(sources, "models"), model_routes.merged(sources, "routes")),
                         found, routing)


def load(path: Path, tool_root: Path | None = None, routing: Mapping[str, Any] | None = None) -> ProjectConfig:
    """读取并校验；文件不存在、无法解析或不合格时抛出 ConfigError。"""
    if not path.is_file():
        raise ConfigError(path, [ConfigIssue(ROOT_KEY, "文件不存在")])
    return parse(layers.read_yaml(path), path, tool_root, routing)
