"""模型别名与路由表(architecture/01 5.2、architecture/02 2.5、2.7)：选择工具与模型的唯一方式。

- `models.<别名>`：tool 必填；model 省略时用工具自己的缺省模型；effort 为推理强度(可省略)；inputUsdPerMTok 与
  outputUsdPerMTok 为每百万 token 的价格(美元)，只在工具不返回费用时用于估算。同一工具的同一模型在不同别名中价格
  不同时视为配置错误；
- `routes.<调用点>`：调用点 → 别名。调用点是固定的清单 CALL_POINTS，每个对应一处真实的模型调用，名称为
  `<环节>.<角色>`(角色名以环节名开头时去掉这一段)；三种条件(CONDITIONS)写成后缀，只用在声明了该条件的调用点上。
  带条件 [c1, c2…] 的调用依次取 `<调用点>.<c1>`、`<调用点>.<c2>`…、`<调用点>`、`default` 中第一条写了的路由；
- 两段写在本机用户配置的顶层与 project.yaml 中，按别名、按路由键合并，project.yaml 的同一项整体替换用户配置的；
  核心不给缺省路由，都没有时在运行时报出调用点；
- 命令行的 --runner 改写工具：工具与别名的不同时不沿用别名的模型与推理强度；--model 改写模型(推理强度不再沿用)。
旧的 agents、capabilities、roleCapabilities、defaultTool 与 stages 中的工具、模型设置已经删去，读到时报 LEGACY_HINT。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.config.layers import ConfigIssue

TOKENS_PER_PRICE_UNIT = 1_000_000
DEFAULT = "default"
HIGH_RISK = "high-risk"
FRONTEND = "frontend"
LARGE = "large"
CONDITIONS = {
    HIGH_RISK: "修复的风险判定为高风险(review.riskRules 命中，或分诊与计划标记)",
    FRONTEND: "改动涉及前端文件(按 stages.fix.roles.frontend-designer.paths 判定)",
    LARGE: "大任务(C 通道)",
}
LEGACY_HINT = ("已不再支持：工具与模型改在顶层 models(模型别名)与 routes(调用点 → 别名)中选择，例如 "
               "`models: {opus: {tool: claude, model: opus}}` 与 `routes: {default: opus, fix.planner: opus}`，"
               "调用点见 `tightrein project config --routes`")
LEGACY_TOP = ("agents", "capabilities", "roleCapabilities", "defaultTool")
LEGACY_STAGE = ("tool", "model", "capability", "refuter", "session", "highRisk")
LEGACY_PART = ("tool", "model", "capability", "highRisk")


@dataclass(frozen=True)
class CallPoint:
    description: str
    conditions: tuple[str, ...] = ()


CALL_POINTS: dict[str, CallPoint] = {
    "collect.static-review": CallPoint("静态巡检的增量审查"),
    "collect.baseline-review": CallPoint("静态巡检的基线审查，每批一次"),
    "collect.variant-scan": CallPoint("以一个缺陷模式为种子的全量扫描"),
    "collect.claim-verifier": CallPoint("静态巡检候选主张的取证"),
    "collect.spec-drafter": CallPoint("没有自动导出时起草接口描述(project spec draft)"),
    "triage.claim-verifier": CallPoint("分诊取证"),
    "triage.refuter": CallPoint("证伪复核；须与 triage.claim-verifier 使用不同的工具或模型"),
    "triage.dedup": CallPoint("分诊查重"),
    "fix.scout": CallPoint("勘察根因位置、联动方与可复用实现", (FRONTEND,)),
    "fix.planner": CallPoint("出修复计划", (HIGH_RISK, LARGE)),
    "fix.frontend-designer": CallPoint("计划含前端文件时的前端设计说明"),
    "fix.executor": CallPoint("写复现测试与写代码(同一会话的两轮)", (HIGH_RISK,)),
    "fix.repro-writer": CallPoint("安全、数据类的复现测试(与写代码不同的会话)"),
    "fix.review.light": CallPoint("轻量评审"),
    "fix.review.deep": CallPoint("深度评审(盲审)；须与 fix.executor 使用不同的工具或模型"),
    "fix.session": CallPoint("修复的终端交互会话"),
    "verify.screenshot-review": CallPoint("合并前验证的截图查看"),
    "learn.lesson-writer": CallPoint("经验撰写与同类条目的矛盾比对"),
    "learn.rule-writer": CallPoint("缺陷变规则"),
    "learn.improvement-writer": CallPoint("改进建议"),
    "learn.knowledge-curator": CallPoint("知识写入的去重判断(由写入知识的环节发起)"),
    "eval.judge": CallPoint("评测的模型评审"),
}
# 须与生成者使用不同工具或模型的调用点：(评审者, 生成者)；两者各自带条件的路由也逐一比较
INDEPENDENT = (("triage.refuter", "triage.claim-verifier"), ("fix.review.deep", "fix.executor"))
# 评测与改进建议中代表一个环节的调用点；没有的环节取 default
PRIMARY = {"triage": "triage.claim-verifier", "fix": "fix.executor"}


def call_point(stage: str, name: str) -> str:
    """角色或任务对应的调用点：角色名以环节名开头时去掉这一段(fix-scout → fix.scout)；不在清单中时抛出 KeyError。"""
    point = f"{stage}.{name.removeprefix(f'{stage}-')}"
    if point not in CALL_POINTS:
        raise KeyError(f"{point} 不是登记的调用点(config.routes.CALL_POINTS)")
    return point


def primary(stage: str) -> str:
    return PRIMARY.get(stage, DEFAULT)


class RouteError(LookupError):
    """别名或路由的配置不完整；key 为出问题的完整键名。"""

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_mtok: float
    output_usd_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        total = input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok
        return total / TOKENS_PER_PRICE_UNIT


@dataclass(frozen=True)
class ModelChoice:
    """选定的工具与模型；model 为空表示使用工具自己的缺省模型。alias 为取自哪个别名，命令行改写了工具或模型时为空。"""

    tool: str
    model: str | None
    effort: str | None = None
    alias: str | None = None


@dataclass(frozen=True)
class Resolved:
    """一次解析：key 为生效的路由键(routes.<键> 中的键)，没有路由时为空。"""

    point: str
    conditions: tuple[str, ...]
    key: str | None
    choice: ModelChoice | None


def no_route(point: str) -> str:
    return (f"调用点 {point} 没有路由：在本机用户配置 ~/.config/tightrein/config.yaml(或 project.yaml)中写 "
            f"routes.default 或 routes.{point}，值为 models 中的别名")


def _alias(name: str, item: Mapping[str, Any]) -> tuple[ModelChoice, ModelPrice | None]:
    price = None
    if "inputUsdPerMTok" in item:
        price = ModelPrice(float(item["inputUsdPerMTok"]), float(item["outputUsdPerMTok"]))
    return ModelChoice(item["tool"], item.get("model"), item.get("effort"), name), price


class Routes:
    """各层合并后的 models 与 routes。同一工具的同一模型在不同别名中价格不同时抛出 RouteError。"""

    def __init__(self, models: Mapping[str, Mapping[str, Any]] | None = None,
                 routes: Mapping[str, str] | None = None) -> None:
        self.aliases: dict[str, ModelChoice] = {}
        self._prices: dict[tuple[str, str], ModelPrice] = {}
        for name, item in (models or {}).items():
            choice, price = _alias(name, item)
            self.aliases[name] = choice
            if price is None or choice.model is None:
                continue
            known = self._prices.get((choice.tool, choice.model))
            if known is not None and known != price:
                raise RouteError(f"models.{name}", f"工具 {choice.tool} 的模型 {choice.model} 的价格与其他别名中的不一致")
            self._prices[(choice.tool, choice.model)] = price
        self.routes = dict(routes or {})

    def alias(self, name: str) -> ModelChoice:
        if name not in self.aliases:
            raise RouteError(f"models.{name}", f"没有别名 {name}")
        return self.aliases[name]

    def lookup(self, point: str, conditions: Sequence[str] = ()) -> str | None:
        """生效的路由键：`<调用点>.<条件>`(按条件的次序)、`<调用点>`、default 中第一个写了的；都没有时为空。"""
        keys = [*(f"{point}.{condition}" for condition in conditions), point, DEFAULT]
        return next((key for key in keys if key in self.routes), None)

    def resolve(self, point: str, conditions: Sequence[str] = ()) -> ModelChoice:
        key = self.lookup(point, conditions)
        if key is None:
            raise RouteError(f"routes.{point}", no_route(point))
        name = self.routes[key]
        if name not in self.aliases:
            raise RouteError(f"routes.{key}", f"别名 {name} 没有在 models 中定义")
        return self.aliases[name]

    def choose(self, point: str, conditions: Sequence[str] = (), *, tool: str | None = None,
               model: str | None = None) -> ModelChoice:
        """按路由解析，再叠加命令行的 --runner(tool)与 --model(model)；改写了工具时没有路由也可以运行。"""
        try:
            found = self.resolve(point, conditions)
        except RouteError:
            if tool is None:
                raise
            return ModelChoice(tool, model)
        if tool is not None and tool != found.tool:
            return ModelChoice(tool, model)
        if model is not None:
            return ModelChoice(found.tool, model)
        return found

    def explain(self, point: str, conditions: Sequence[str] = ()) -> Resolved:
        key = self.lookup(point, conditions)
        name = None if key is None else self.routes[key]
        return Resolved(point, tuple(conditions), key, self.aliases.get(name) if name is not None else None)

    def variants(self, point: str) -> list[tuple[str, ...]]:
        """该调用点无条件的一项与写了路由的各条件变体。"""
        return [(), *((condition,) for condition in CALL_POINTS[point].conditions
                      if f"{point}.{condition}" in self.routes)]

    def price(self, tool: str, model: str) -> ModelPrice | None:
        return self._prices.get((tool, model))

    def estimate_cost(self, tool: str, model: str | None, input_tokens: int, output_tokens: int) -> float | None:
        """工具不返回费用时按价格估算；模型为空或没有价格时返回 None。"""
        if model is None:
            return None
        price = self.price(tool, model)
        return None if price is None else price.cost(input_tokens, output_tokens)


def merged(sources: Sequence[Mapping[str, Any]], section: str) -> dict[str, Any]:
    """各层的 models 或 routes 按项合并，sources 按优先次序排列，上层的一项整体替换下层的同一项。"""
    found: dict[str, Any] = {}
    for source in reversed(sources):
        found.update(source.get(section) or {})
    return found


def _key_issue(key: str) -> str | None:
    if key == DEFAULT or key in CALL_POINTS:
        return None
    point, _, condition = key.rpartition(".")
    if point in CALL_POINTS:
        allowed = CALL_POINTS[point].conditions
        if condition in allowed:
            return None
        if condition in CONDITIONS:
            return f"调用点 {point} 不用条件 {condition}" + (f"，可用的条件：{'、'.join(allowed)}" if allowed else "")
    return "不认识的调用点；调用点与条件见 `tightrein project config --routes`"


def _independence(routes: Routes) -> Iterator[ConfigIssue]:
    for reviewer, producer in INDEPENDENT:
        for reviewer_conditions in routes.variants(reviewer):
            for producer_conditions in routes.variants(producer):
                try:
                    left = routes.resolve(reviewer, reviewer_conditions)
                    right = routes.resolve(producer, producer_conditions)
                except RouteError:
                    continue  # 没有路由在运行时报出，别名不存在在别处报出
                if (left.tool, left.model) != (right.tool, right.model):
                    continue
                line = ".".join((reviewer, *reviewer_conditions))
                other = ".".join((producer, *producer_conditions))
                yield ConfigIssue(f"routes.{line}",
                                  f"须与 {other} 使用不同的工具或模型(两者都解析为工具 {left.tool}、模型 {left.model})："
                                  f"把 routes.{line} 改为(没写时写上)另一个工具或模型的别名")


def layer_issues(data: Mapping[str, Any]) -> list[ConfigIssue]:
    """一层(本机用户配置或 project.yaml)自身的检查：路由键是登记的调用点及其条件，同一模型在各别名中价格一致。"""
    found = [ConfigIssue(f"routes.{key}", reason) for key in data.get("routes") or {}
             if (reason := _key_issue(key)) is not None]
    try:
        Routes(data.get("models"))
    except RouteError as error:
        found.append(ConfigIssue(error.key, error.reason))
    return found


def issues(sources: Sequence[Mapping[str, Any]]) -> list[ConfigIssue]:
    """各层合并后的检查：各层自身的检查、引用的别名存在、合并后价格一致、评审与生成者独立。"""
    found = [issue for source in sources for issue in layer_issues(source)]
    models = merged(sources, "models")
    table = merged(sources, "routes")
    found += [ConfigIssue(f"routes.{key}", f"别名 {name} 没有在 models 中定义")
              for key, name in table.items() if name not in models]
    try:
        routes = Routes(models, table)
    except RouteError as error:
        return sorted({*found, ConfigIssue(error.key, error.reason)})
    return sorted({*found, *_independence(routes)})


def legacy_issues(data: Mapping[str, Any], prefix: str = "") -> list[ConfigIssue]:
    """旧的选模型配置(agents、capabilities、roleCapabilities、defaultTool、evaluation.judge 与 stages 中的工具、
    模型、能力档、refuter、session)：报出完整键名与新写法，代替笼统的结构错误。"""
    keys = [key for key in LEGACY_TOP if key in data]
    if isinstance(data.get("evaluation"), Mapping) and "judge" in data["evaluation"]:
        keys.append("evaluation.judge")
    for stage, node in (data.get("stages") or {}).items() if isinstance(data.get("stages"), Mapping) else ():
        if not isinstance(node, Mapping):
            continue
        base = f"stages.{stage}"
        keys += [f"{base}.{key}" for key in LEGACY_STAGE if key in node]
        parts = [(f"{base}.{group}.{name}", item) for group in ("roles", "tasks")
                 for name, item in (node.get(group) or {}).items()]
        parts += [(f"{base}.review.{name}", item) for name, item in (node.get("review") or {}).items()]
        if "screenshotReview" in node:
            parts.append((f"{base}.screenshotReview", node["screenshotReview"]))
        keys += [f"{key}.{name}" for key, item in parts if isinstance(item, Mapping)
                 for name in LEGACY_PART if name in item]
    return [ConfigIssue(f"{prefix}{key}", LEGACY_HINT) for key in keys]
