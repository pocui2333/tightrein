"""配置的读取与合并(settings/README.md)。

读取顺序：`settings/defaults.json` → `settings/controls.json`(本机) → 工作区 `settings.json` 的 `overrides`，后者覆盖前者：
- 映射按键递归合并；单个值与列表由上层整体替换；
- 键名后加 `+`(如 `forbidden+`)表示追加到下层列表后；
- 只许追加的列表(受保护文件两级)上层写原键即报错，缺省项删不掉；
- 控制字段按「小步骤 → 模块 → 阶段 → `*`」继承(protocol/naming.key_chain)。

校验一次列出全部问题，每条带完整键名；取不到的键抛 MissingSetting，不悄悄给缺省值。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from tightrein.agents.params import CONDITIONS, Limits, Model
from tightrein.protocol.naming import check_control_key, key_chain, parse_duration
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

APPEND_ONLY = ("boundaries.protected.forbidden", "boundaries.protected.highRisk")
CONTROL_FIELDS: dict[str, type | tuple[type, ...]] = {
    "model": str,
    "modelWhen": dict,
    "fallback": (str, type(None)),
    "timeout": str,
    "sourceTimeout": str,
    "idle": str,
    "turns": int,
    "inputTokens": int,
    "outputTokens": int,
    "rounds": int,
    "access": str,
    "network": bool,
}
DURATION_FIELDS = ("timeout", "idle", "sourceTimeout")
ROUTE_FIELDS = ("model", "modelWhen", "fallback")
MODEL_FIELDS = ("tool", "model", "effort", "price")
PRICE_FIELDS = ("input", "output", "cacheRead", "cacheWrite")
# 登记的调用点：每个调用点都有提示模板 src/tightrein/prompts/<调用点>.md(prompts/README.md)
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
NOT_POINTS = ("README", "TEMPLATE")
PROJECT_FIELDS = ("repo", "mainBranch", "language", "commands", "testPatterns", "frontendPatterns")
LANGUAGES = ("zh", "en", "ja")


class MissingSetting(KeyError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        return f"配置中没有 {self.key}"


class SettingsInvalid(Exception):
    def __init__(self, issues: list[str]) -> None:
        super().__init__("配置有问题：\n" + "\n".join(f"- {issue}" for issue in issues))
        self.issues = issues


@dataclass(frozen=True)
class Layer:
    name: str  # defaults、controls、workspace
    path: Path | None
    data: dict[str, Any]


@dataclass(frozen=True)
class ProjectFacts:
    """项目事实：工作区 settings.json 的 `project` 部分；探测不到的为 None。"""

    repo: Path | None
    main_branch: str | None
    language: str
    commands: dict[str, str | None] = field(default_factory=dict)  # test、lint、build、typecheck
    test_patterns: tuple[str, ...] = ()
    frontend_patterns: tuple[str, ...] = ()


class Settings:
    def __init__(self, layers: list[Layer], project: ProjectFacts | None, sites: dict[str, Any]) -> None:
        self.layers = layers
        self.project = project
        self.sites = sites
        self.merged: dict[str, Any] = {}
        for layer in layers:
            self.merged = merge(self.merged, layer.data, path="")
        self.hash = hashlib.sha256(json.dumps(self.merged, sort_keys=True).encode()).hexdigest()[:12]

    @classmethod
    def load(cls, tool: ToolLayout, workspace: WorkspaceLayout | None = None) -> Settings:
        layers = [Layer("defaults", tool.defaults, _read(tool.defaults, required=True))]
        if tool.controls.exists():
            layers.append(Layer("controls", tool.controls, _read(tool.controls, required=True)))
        project: ProjectFacts | None = None
        sites = _read(tool.sites, required=False)
        if workspace is not None and workspace.settings.exists():
            data = _read(workspace.settings, required=True)
            layers.append(Layer("workspace", workspace.settings, data.get("overrides") or {}))
            project, project_issues = _project(data.get("project") or {}, workspace.settings.parent)
        else:
            project_issues = []
        if workspace is not None:
            sites = merge(sites, _read(workspace.sites, required=False), path="")
        settings = cls(layers, project, sites)
        issues = project_issues + settings.issues()
        if issues:
            raise SettingsInvalid(issues)
        return settings

    @classmethod
    def from_data(cls, *data: dict[str, Any], project: ProjectFacts | None = None,
                  sites: dict[str, Any] | None = None) -> Settings:
        """测试与试跑用：直接给各层数据。"""
        return cls([Layer(f"layer{index}", None, item) for index, item in enumerate(data)], project, sites or {})

    # 读取
    def get(self, key: str) -> Any:
        node: Any = self.merged
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise MissingSetting(key)
            node = node[part]
        return node

    def duration(self, key: str) -> float:
        return parse_duration(self.get(key))

    def section(self, key: str) -> dict[str, Any]:
        """模块参数：controls 中该键自身的取值(不继承)。"""
        return dict(self.merged.get("controls", {}).get(check_control_key(key), {}))

    def control(self, key: str, name: str) -> Any:
        controls = self.merged.get("controls", {})
        for level in key_chain(key):
            if name in controls.get(level, {}):
                return controls[level][name]
        raise MissingSetting(f"controls.{key}.{name}")

    def model(self, alias: str) -> Model:
        spec = self.merged.get("models", {}).get(alias)
        if spec is None:
            raise MissingSetting(f"models.{alias}")
        price = spec.get("price") or {}
        return Model(
            alias=alias,
            tool=spec["tool"],
            model=spec.get("model") or "",
            effort=spec.get("effort"),
            price_input=price.get("input"),
            price_output=price.get("output"),
            price_cache_read=price.get("cacheRead"),
            price_cache_write=price.get("cacheWrite"),
        )

    def model_for(self, point: str, conditions: tuple[str, ...] = ()) -> Model:
        """沿控制键由近到远：每一级先查条件(modelWhen)再查 model，查到即停。

        更近一级写了 model 时不再沿用上级的条件：`implement.design.frontend` 不会因 high_risk 取到上级 `implement.design` 的条件模型。
        """
        for level in self._model_levels(point):
            when = level.get("modelWhen") or {}
            for condition in conditions:
                if condition in when:
                    return self.model(when[condition])
            if "model" in level:
                return self.model(level["model"])
        raise MissingSetting(f"controls.{point}.model")

    def _model_levels(self, point: str) -> list[dict[str, Any]]:
        """从近到远、直到第一个写了 model 的层级为止的各级取值。"""
        controls = self.merged.get("controls", {})
        levels = []
        for key in key_chain(point):
            level = controls.get(key, {})
            levels.append(level)
            if "model" in level:
                break
        return levels

    def fallback_for(self, point: str) -> Model | None:
        alias = self.control(point, "fallback")
        return None if alias is None else self.model(alias)

    def limits_for(self, point: str) -> Limits:
        return Limits(
            timeout_s=parse_duration(self.control(point, "timeout")),
            turns=self.control(point, "turns"),
            output_tokens=self.control(point, "outputTokens"),
            input_tokens=self.control(point, "inputTokens"),
            idle_s=parse_duration(self.control(point, "idle")),
        )

    def explain(self, key: str) -> list[tuple[str, Any]]:
        """`project config --explain`：每一层给出的值(含 `+` 追加)。"""
        found: list[tuple[str, Any]] = []
        parts = key.split(".")
        # 控制键本身带点：controls.implement.code.turns → controls["implement.code"]["turns"]
        if parts[0] == "controls" and len(parts) > 2:
            parts = ["controls", ".".join(parts[1:-1]), parts[-1]]
        for layer in self.layers:
            node: Any = layer.data
            for part in parts[:-1]:
                node = node.get(part, {}) if isinstance(node, dict) else {}
            if isinstance(node, dict):
                for name in (parts[-1], parts[-1] + "+"):
                    if name in node:
                        found.append((layer.name, node[name]))
        return found

    # 校验
    def issues(self) -> list[str]:
        """先查结构(不认识的键、缺必填、类型、引用的别名)；结构不合格时不做后续语义检查，避免在缺键的数据上误报。"""
        issues = self._structure_issues()
        if issues:
            return issues
        for key, values in self.merged.get("controls", {}).items():
            issues += _route_issues(key, values)
        issues += self._independence_issues()
        issues += self._threshold_issues()
        issues += self._price_issues()
        return issues

    def _structure_issues(self) -> list[str]:
        issues: list[str] = []
        known = set(self.layers[0].data) if self.layers else set()
        for layer in self.layers[1:]:
            issues += _append_only_issues(layer)
            issues += [f"{key.removesuffix('+')}（{layer.name}）：不认识的键" for key in layer.data
                       if key.removesuffix("+") not in known]
        for alias, spec in self.merged.get("models", {}).items():
            issues += _model_issues(alias, spec)
        issues += _gate_issues(self.merged.get("boundaries", {}).get("gates") or {})
        controls = self.merged.get("controls", {})
        if "*" not in controls:
            issues.append("controls.*：缺少全局缺省")
        aliases = set(self.merged.get("models", {}))
        for key, values in controls.items():
            if key != "*":
                try:
                    check_control_key(key)
                except ValueError as error:
                    issues.append(f"controls.{key}：{error}")
                    continue
            issues += _control_issues(key, values, aliases)
        return issues

    def _price_issues(self) -> list[str]:
        """同一工具同一模型在不同别名中价格必须一致，否则按价格估算的费用前后对不上。"""
        seen: dict[tuple[str, str], tuple[str, Any]] = {}
        issues = []
        for alias, spec in self.merged.get("models", {}).items():
            price = spec.get("price")
            if price is None:
                continue
            key = (spec.get("tool", ""), spec.get("model") or "")
            if key in seen and seen[key][1] != price:
                issues.append(f"models.{alias}.price：与 {seen[key][0]} 同为 {key[0]}/{key[1]}，价格不一致")
            seen.setdefault(key, (alias, price))
        return issues

    def _independence_issues(self) -> list[str]:
        """审查者与生成者必须用不同的模型：按解析后的实际模型比较，逐一比较各条件变体。"""
        issues = []
        for reviewer, producer in self.merged.get("independence", []):
            for left in self._variants(reviewer):
                for right in self._variants(producer):
                    if (left.tool, left.model) == (right.tool, right.model):
                        issues.append(f"controls.{reviewer}：与 {producer} 用了同一个模型 {left.tool}/{left.model}")
        return issues

    def _variants(self, point: str) -> list[Model]:
        try:
            models = [self.model_for(point)]
            for level in self._model_levels(point):
                models += [self.model(alias) for alias in (level.get("modelWhen") or {}).values()]
        except MissingSetting:
            return []
        return models

    def _threshold_issues(self) -> list[str]:
        issues = []
        try:
            cap, auto = self.get("boundaries.changeCap"), self.get("boundaries.autoApprove")
            for name in ("files", "lines"):
                if auto[name] > cap[name]:
                    issues.append(f"boundaries.autoApprove.{name}：{auto[name]} 超过改动量上限 {cap[name]}")
        except MissingSetting as error:
            issues.append(str(error))
        # 下级时限之和不超过上级：各小步骤之和 ≤ 阶段(对象级)，各阶段 ≤ 一次完整运行。格式错的值已在字段检查中报过，这里跳过
        controls = self.merged.get("controls", {})
        run = _seconds(self.merged.get("limits", {}).get("timeouts", {}).get("run"))
        for stage in ("collect", "assess", "implement"):
            upper = _seconds(controls.get(stage, {}).get("timeout"))
            if upper is None:
                continue
            steps = {key: _seconds(values.get("timeout")) for key, values in controls.items() if key.startswith(stage + ".")}
            total = sum(value for value in steps.values() if value is not None)
            if total > upper:
                issues.append(f"controls.{stage}.timeout：下级时限之和 {total / 60:g}m 超过上级 {controls[stage]['timeout']}")
            if run is not None and upper > run:
                issues.append(f"controls.{stage}.timeout：超过一次完整运行的时限 {self.merged['limits']['timeouts']['run']}")
        return issues


def merge(lower: dict[str, Any], upper: dict[str, Any], *, path: str) -> dict[str, Any]:
    result = copy.deepcopy(lower)
    for key, value in upper.items():
        if key.endswith("+"):
            base = key[:-1]
            existing = result.get(base, [])
            if not isinstance(existing, list) or not isinstance(value, list):
                raise SettingsInvalid([f"{path}{key}：`+` 只能用于列表"])
            result[base] = existing + [item for item in value if item not in existing]
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value, path=f"{path}{key}.")
        else:
            result[key] = copy.deepcopy(value)
    return result


def _append_only_issues(layer: Layer) -> list[str]:
    issues = []
    for key in APPEND_ONLY:
        node: Any = layer.data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict) and parts[-1] in node:
            issues.append(f"{key}（{layer.name}）：只能用 `{parts[-1]}+` 追加，不能替换缺省的受保护路径")
    return issues


def _gate_issues(gates: dict[str, Any]) -> list[str]:
    """必须人工的关卡写死在 protocol/boundaries.MANDATORY_GATES，配置里写了就报错，不悄悄忽略。"""
    from tightrein.protocol.boundaries import MANDATORY_GATES

    return [f"boundaries.gates.{name}：必须人工的关卡，不能配置" for name in sorted(gates) if name in MANDATORY_GATES]


def _model_issues(alias: str, spec: Any) -> list[str]:
    if not isinstance(spec, dict):
        return [f"models.{alias}：必须是对象"]
    issues = [f"models.{alias}.tool：缺少"] if "tool" not in spec else []
    issues += [f"models.{alias}.{name}：不认识的键" for name in spec if name not in MODEL_FIELDS]
    price = spec.get("price")
    if isinstance(price, dict):
        issues += [f"models.{alias}.price.{name}：不认识的键" for name in price if name not in PRICE_FIELDS]
    return issues


def _control_issues(key: str, values: dict[str, Any], aliases: set[str]) -> list[str]:
    issues = []
    for name, value in values.items():
        expected = CONTROL_FIELDS.get(name)
        if expected is None:
            continue  # 模块参数，由各模块按自己的 schema 校验
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            issues.append(f"controls.{key}.{name}：类型不对：{value!r}")
            continue
        if name in DURATION_FIELDS and isinstance(value, str):
            try:
                parse_duration(value)
            except ValueError as error:
                issues.append(f"controls.{key}.{name}：{error}")
        if name in ("model", "fallback") and value is not None and value not in aliases:
            issues.append(f"controls.{key}.{name}：没有这个模型别名：{value}")
        if name == "modelWhen" and isinstance(value, dict):
            issues += [f"controls.{key}.modelWhen.{cond}：没有这个模型别名：{alias}"
                       for cond, alias in value.items() if alias not in aliases]
        if name == "access" and value not in ("read", "write"):
            issues.append(f"controls.{key}.access：只能是 read 或 write")
    return issues


@cache
def call_points() -> frozenset[str]:
    return frozenset(path.stem for path in PROMPTS_DIR.glob("*.md") if path.stem not in NOT_POINTS)


def _route_issues(key: str, values: dict[str, Any]) -> list[str]:
    """写了 model、modelWhen、fallback 的控制键只能是登记的调用点或其上级；modelWhen 的条件只能是覆盖到的调用点声明过的。"""
    if not any(name in values for name in ROUTE_FIELDS):
        return []
    covered = [point for point in call_points() if key == "*" or point == key or point.startswith(key + ".")]
    if not covered:
        return [f"controls.{key}：不是登记的调用点(src/tightrein/prompts/<调用点>.md)，也不是其上级，不能写 "
                + "、".join(name for name in ROUTE_FIELDS if name in values)]
    when = values.get("modelWhen")
    if not isinstance(when, dict):
        return []
    allowed = sorted({condition for point in covered for condition in CONDITIONS.get(point, ())})
    hint = "、".join(allowed) if allowed else "无(该调用点不按条件选模型)"
    return [f"controls.{key}.modelWhen.{condition}：没有声明这个条件，{key} 可用的条件：{hint}"
            for condition in when if condition not in allowed]


def _seconds(value: Any) -> float | None:
    try:
        return parse_duration(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _project(data: dict[str, Any], workspace_root: Path) -> tuple[ProjectFacts | None, list[str]]:
    issues = [f"project.{name}：缺少(探测不到时写 null)" for name in PROJECT_FIELDS if name not in data]
    language = data.get("language") or "zh"
    if language not in LANGUAGES:
        issues.append(f"project.language：只能是 {'、'.join(LANGUAGES)}")
    if issues:
        return None, issues
    repo = data.get("repo")
    return ProjectFacts(
        repo=(workspace_root / repo).resolve() if repo else None,
        main_branch=data.get("mainBranch"),
        language=language,
        commands=dict(data.get("commands") or {}),
        test_patterns=tuple(data.get("testPatterns") or ()),
        frontend_patterns=tuple(data.get("frontendPatterns") or ()),
    ), []


def _read(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise SettingsInvalid([f"{path}：文件不存在"])
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SettingsInvalid([f"{path}：不是合法的 JSON（第 {error.lineno} 行）"]) from error
    if not isinstance(data, dict):
        raise SettingsInvalid([f"{path}：顶层必须是对象"])
    return data
