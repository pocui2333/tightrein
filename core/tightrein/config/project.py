"""读取与校验 project.yaml(architecture/01 5.2)。

校验一次列出全部问题，每条带完整键名，例如 `accounts.login.tokenPath`：
1. 按 config/project-config.schema.json 校验结构：缺少的必填项与不认识的键报到该键本身，其余报到出错的值；
2. thresholds 中每个 `{value, min, max}` 满足 min ≤ value ≤ max；各层合并后 thresholds.autonomy.planMaxFiles、planMaxLines
   不大于 thresholds.change.maxFiles、maxLines；
3. 各环节与评测引用的能力档在对应工具上有模型，写了工具的角色设置所用的档在该工具上有模型；证伪复核与深度评审所用的
   工具或模型与生成者不同，按解析后的实际工具与模型比较；
4. extensions.<扩展点> 的 use 与 command 不同时出现。
结构校验用展开引用后的 schema 直接取得 jsonschema 的结构化错误，从中得到缺少的键名，不解析错误文字。

缺省值来自三层：核心 config/defaults.yaml、stacks 所列技术栈的 defaults.yaml(config.layers)与本机用户配置的
agents 段(用户 agent 层，config.user)；get、tunable 与 method_options 依次取 project.yaml、用户 agent 层、
技术栈层(后列的技术栈优先)与核心层。用户 agent 层只有 defaultTool、stages 的工具与模型部分、capabilities 与
roleCapabilities，是个人缺省，所以排在 project.yaml 之下，项目需要时可以覆盖。

工具与模型按合并后的生效值读取(stage_setting)：各层的 stages.<环节> 按键递归合并，上层改写了工具而没有写模型时，
下层的模型属于原工具，不再沿用；合并后没有工具时取 defaultTool。核心不给缺省工具，都没有时在运行时报出完整键名。
capabilities 按档、工具逐项合并，同一档同一工具的一项由上层整体替换。
角色与任务可在 stages.<环节>.roles|tasks.<名称> 中写 tool、model、capability(用户 agent 层也可写)，按同样的规则合并
(role_agent)，填进执行器任务后优先于环节设置。

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
from tightrein.config.capabilities import Capabilities, CapabilityError, ModelChoice
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
from tightrein.domain.enums import ExtensionMode, ExtensionPoint, LogLevel, Stage
from tightrein.store.files.layout import ToolLayout
from tightrein.store.retention import RetentionPolicy

__all__ = ["MISSING_REASON", "ROOT_KEY", "UNKNOWN_REASON", "ConfigError", "ConfigIssue"]

STACK_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

# 须与生成者使用不同工具或模型的设置：(环节, 设置的点分路径, 生成者角色, 评审者角色)。与运行时的取法一致：
# 生成者为该角色的模型档在环节工具上的模型；评审者为该设置，设置中没有模型与档时取评审者角色的模型档。
INDEPENDENT_REVIEWERS: tuple[tuple[Stage, str, str, str | None], ...] = (
    (Stage.TRIAGE, "refuter", "claim-verifier", "refuter"),
    (Stage.FIX, "review.deep", "fix-executor", None),
)
MODEL_SETTINGS = ("review.light", "review.deep", "screenshotReview", "refuter", "session")
DEFAULT_TOOL = "defaultTool"
AGENT_KEYS = ("tool", "model", "capability")
ROLE_GROUPS = ("roles", "tasks")
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


def _part(setting: Mapping[str, Any], part: str) -> Mapping[str, Any] | None:
    """环节设置中的一部分(点分路径)；没有工具时沿用环节的工具。"""
    try:
        found = layers.lookup(setting, part)
    except KeyError:
        return None
    if "tool" in found or "tool" not in setting:
        return found
    return {"tool": setting["tool"], **found}


def _model_settings(stages: Mapping[str, Mapping[str, Any]]) -> Iterator[tuple[Mapping[str, Any], str]]:
    for stage, setting in stages.items():
        yield setting, f"stages.{stage}"
        for part in MODEL_SETTINGS:
            found = _part(setting, part)
            if found is not None:
                yield found, f"stages.{stage}.{part}"


def _without_model(node: Mapping[str, Any], parts: Sequence[str]) -> dict[str, Any]:
    found = dict(node)
    if not parts:
        found.pop("model", None)
    else:
        found[parts[0]] = _without_model(found[parts[0]], parts[1:])
    return found


def _merge_setting(lower: Mapping[str, Any], upper: Mapping[str, Any]) -> dict[str, Any]:
    merged = layers.merge(lower, upper)
    for part in ("", *MODEL_SETTINGS):
        try:
            above = layers.lookup(upper, part) if part else upper
        except KeyError:
            continue
        if isinstance(above, Mapping) and "tool" in above and "model" not in above:
            merged = _without_model(merged, part.split(".") if part else ())
    return merged


def default_tool(sources: Sequence[Mapping[str, Any]]) -> str | None:
    """各层中第一个给出的 defaultTool；sources 按优先次序排列。"""
    return next((source[DEFAULT_TOOL] for source in sources if source.get(DEFAULT_TOOL) is not None), None)


def stage_setting(sources: Sequence[Mapping[str, Any]], stage: str) -> dict[str, Any]:
    """各层合并后的 stages.<环节>；没有工具时填入 defaultTool。sources 按优先次序排列。"""
    found: dict[str, Any] = {}
    for source in reversed(sources):
        found = _merge_setting(found, source.get("stages", {}).get(stage, {}))
    tool = default_tool(sources)
    if "tool" not in found and tool is not None:
        found["tool"] = tool
    return found


def merged_capabilities(sources: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """各层 capabilities 按档、工具逐项合并，上层的一项整体替换下层的同一项。"""
    found: dict[str, dict[str, Any]] = {}
    for source in reversed(sources):
        for capability, tools in source.get("capabilities", {}).items():
            found.setdefault(capability, {}).update(tools)
    return found


def role_capability(sources: Sequence[Mapping[str, Any]], stage: Stage, role: str) -> str | None:
    """角色或任务的模型档：各层中 stages.<环节>.roles|tasks.<角色>.capability 优先，其次 roleCapabilities.<角色>；
    sources 按优先次序排列。"""
    for group in ("roles", "tasks"):
        for source in sources:
            found = source.get("stages", {}).get(stage.value, {}).get(group, {}).get(role, {}).get("capability")
            if found is not None:
                return found
    for source in sources:
        found = source.get("roleCapabilities", {}).get(role)
        if found is not None:
            return found
    return None


def _choice_of(node: Any) -> dict[str, Any]:
    return {key: node[key] for key in AGENT_KEYS if key in node} if isinstance(node, Mapping) else {}


def _layered_choice(sources: Sequence[Mapping[str, Any]], stage: Stage, name: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for source in reversed(sources):
        node = source.get("stages", {}).get(stage.value, {})
        for group in ROLE_GROUPS:
            found = _merge_setting(found, _choice_of(node.get(group, {}).get(name, {})))
    return found


def role_agent(sources: Sequence[Mapping[str, Any]], stage: Stage, name: str,
               overlay: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """角色或任务的工具、模型与模型档：各层 stages.<环节>.roles|tasks.<名称> 中这三项按环节设置的规则合并(上层改写了
    工具而没有写模型时，不沿用下层的模型)；overlay 给出时再叠加它(例如 stages.triage.refuter)。只有模型而没有工具时，
    工具取环节的工具。sources 按优先次序排列。"""
    found = _layered_choice(sources, stage, name)
    if overlay:
        found = _merge_setting(found, _choice_of(overlay))
    if found.get("model") is not None and found.get("tool") is None:
        tool = stage_setting(sources, stage.value).get("tool")
        if tool is not None:
            found["tool"] = tool
    return found


def role_choice(sources: Sequence[Mapping[str, Any]], capabilities: Capabilities, stage: Stage, name: str,
                overlay: Mapping[str, Any] | None = None) -> ModelChoice:
    """按运行时的取法解析角色实际使用的工具与模型：角色设置的工具、模型与档优先，没有档时取 role_capability，
    再次为环节设置。"""
    agent = role_agent(sources, stage, name, overlay)
    capability = agent.get("capability") or role_capability(sources, stage, name)
    return capabilities.choose(stage_setting(sources, stage.value), f"stages.{stage.value}", tool=agent.get("tool"),
                               model=agent.get("model"), capability=capability)


def _independence_issue(stage: Stage, part: str, setting: Mapping[str, Any], producer_role: str,
                        reviewer_role: str | None, capabilities: Capabilities,
                        sources: Sequence[Mapping[str, Any]]) -> ConfigIssue | None:
    key = f"stages.{stage.value}.{part}"
    try:
        if reviewer_role is not None:
            # 评审者是评审者角色，评审设置叠加在角色设置之上
            try:
                overlay = layers.lookup(setting, part)
            except KeyError:
                overlay = None
            reviewer = role_choice(sources, capabilities, stage, reviewer_role, overlay=overlay)
        else:
            found = _part(setting, part)
            if found is None or "tool" not in found:
                return None
            reviewer = capabilities.choose(found, key)
        producer = role_choice(sources, capabilities, stage, producer_role)
    except CapabilityError:
        return None  # 引用无法解析的能力档在别处报出，或在运行时报出
    if (producer.tool, producer.model) != (reviewer.tool, reviewer.model):
        return None
    return ConfigIssue(key, f"须与生成者 {producer_role}(工具 {producer.tool}，模型 {producer.model})使用不同的工具或模型："
                            f"只用一种工具时给两者不同的模型档，例如 {key}.capability: standard；"
                            f"用两种工具时可写 {key}.tool 为另一种工具")


def _role_issues(sources: Sequence[Mapping[str, Any]], capabilities: Capabilities) -> Iterator[ConfigIssue]:
    """写了工具的角色设置：所用的档或模型须在该工具上有映射。"""
    names = sorted({(stage, group, name) for source in sources
                    for stage, node in source.get("stages", {}).items()
                    for group in ROLE_GROUPS for name in node.get(group, {})})
    for stage, group, name in names:
        if _layered_choice(sources, Stage(stage), name).get("tool") is None:
            continue
        try:
            role_choice(sources, capabilities, Stage(stage), name)
        except CapabilityError as error:
            yield ConfigIssue(f"stages.{stage}.{group}.{name}", str(error))


def _model_issues(sources: Sequence[Mapping[str, Any]], capabilities: Capabilities) -> Iterator[ConfigIssue]:
    """按各层合并后的生效值检查；没有解析出工具的设置不在加载时报错，运行时报出完整键名。"""
    names = sorted({stage for source in sources for stage in source.get("stages", {})})
    stages = {stage: stage_setting(sources, stage) for stage in names}
    for setting, key in _model_settings(stages):
        if setting.get("tool") is None:
            continue
        try:
            capabilities.choose(setting, key)
        except CapabilityError as error:
            yield ConfigIssue(key, str(error))
    judge = _first(sources, "evaluation.judge")
    runner = judge.get("runner") or default_tool(sources)
    if runner is not None:
        try:
            capabilities.choose({"tool": runner, "capability": judge.get("capability")}, "evaluation.judge")
        except CapabilityError as error:
            yield ConfigIssue("evaluation.judge", str(error))
    for stage, part, producer_role, reviewer_role in INDEPENDENT_REVIEWERS:
        issue = _independence_issue(stage, part, stages.get(stage.value, {}), producer_role, reviewer_role,
                                    capabilities, sources)
        if issue is not None:
            yield issue
    yield from _role_issues(sources, capabilities)


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
    issues = sorted({issue for error in layers.validator().iter_errors(data) for issue in layers.schema_issues(error)})
    if issues:
        return issues
    sources = [data, *(defaults or [layers.core_defaults()])]
    try:
        capabilities = Capabilities(merged_capabilities(sources))
    except CapabilityError as error:
        return [ConfigIssue(error.key, error.reason)]
    return sorted([*_threshold_issues(data.get("thresholds", {})),
                   *_size_issues(sources),
                   *_model_issues(sources, capabilities),
                   *_extension_issues(data.get("extensions", {}))])


def core_config() -> ProjectConfig:
    """只含核心缺省值的配置，供不需要工作区的命令(install、third-party、skills check)读取可调项。"""
    return ProjectConfig(layers.DEFAULTS_FILE, {}, Capabilities())


@dataclass(frozen=True)
class ProjectConfig:
    """校验通过的 project.yaml。data 为读出的原始映射，agents 为本机用户配置的 agents 段，调用方只读不改。"""

    path: Path
    data: Mapping[str, Any]
    capabilities: Capabilities
    stack_layers: tuple[Layer, ...] = ()
    agents: Mapping[str, Any] = field(default_factory=dict)

    def sources(self) -> list[Mapping[str, Any]]:
        """按优先次序排列的各层：project.yaml、用户 agent 层、技术栈层(后列的优先)、核心层。"""
        return [self.data, self.agents, *(layer.data for layer in reversed(self.stack_layers)),
                layers.core_defaults()]

    @property
    def default_tool(self) -> str | None:
        return default_tool(self.sources())

    def stage_setting(self, stage: Stage) -> dict[str, Any]:
        """各层合并后的 stages.<环节>(工具、模型、评审设置等)，没有工具时已填入 defaultTool。"""
        return stage_setting(self.sources(), stage.value)

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

    def role_agent(self, stage: Stage, name: str, overlay: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """角色或任务的工具、模型与模型档(stages.<环节>.roles|tasks.<名称>，各层合并)，见模块函数 role_agent。"""
        return role_agent(self.sources(), stage, name, overlay)

    def role_capability(self, stage: Stage, role: str) -> str | None:
        """角色或任务的模型档(design 9.6)：stages.<环节>.roles|tasks.<角色>.capability，其次 roleCapabilities。"""
        return role_capability(self.sources(), stage, role)

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

    def model_choice(
        self,
        stage: Stage,
        part: str | None = None,
        *,
        tool: str | None = None,
        model: str | None = None,
        capability: str | None = None,
    ) -> ModelChoice:
        """某环节(part 为 MODEL_SETTINGS 中的点分路径时取其中的设置，没有工具时沿用环节的工具)的工具与模型，
        显式参数优先。"""
        key = f"stages.{stage.value}" if part is None else f"stages.{stage.value}.{part}"
        setting = self.stage_setting(stage)
        if part is not None:
            setting = _part(setting, part) or {}
        return self.capabilities.choose(setting, key, tool=tool, model=model, capability=capability)


def parse(data: Any, path: Path, tool_root: Path | None = None,
          agents: Mapping[str, Any] | None = None) -> ProjectConfig:
    """校验 project.yaml 的内容并读取 stacks 所列技术栈的缺省层；tool_root 缺省为核心所在仓库，agents 为本机用户配置的
    agents 段。"""
    stacks = data.get("stacks", ()) if isinstance(data, Mapping) else ()
    names = [name for name in stacks if isinstance(name, str) and STACK_NAME.fullmatch(name)] \
        if isinstance(stacks, list) else []
    tool = ToolLayout(tool_root) if tool_root is not None else ToolLayout()
    found = layers.stack_layers(tool.stacks_dir(), names)
    agents = agents or {}
    defaults = [agents, *(layer.data for layer in reversed(found)), layers.core_defaults()]
    issues = check(data, defaults)
    if not issues:
        issues = layers.stack_conflicts(found, data)
    if issues:
        raise ConfigError(path, issues)
    return ProjectConfig(path, data, Capabilities(merged_capabilities([data, *defaults])), found, agents)


def load(path: Path, tool_root: Path | None = None, agents: Mapping[str, Any] | None = None) -> ProjectConfig:
    """读取并校验；文件不存在、无法解析或不合格时抛出 ConfigError。"""
    if not path.is_file():
        raise ConfigError(path, [ConfigIssue(ROOT_KEY, "文件不存在")])
    return parse(layers.read_yaml(path), path, tool_root, agents)
