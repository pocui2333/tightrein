"""风险判定：决定方案用不用高风险模型、要不要等用户确认方案、审查要不要加深度审查。

判两次(多判一次只花程序的时间，不调用模型)：
- 方案前按代码笔记涉及的文件(根因加联动)判定，作为方案的输入与选模型的条件；方案通过后按方案的文件、标记与迁移
  再判一次，定案据此决定是否等用户；
- 自检通过后按实际改动的增删行再判一次(apply_risk，审查调用)，决定审查深度。

任一条命中即为高风险：

| 类别 | 按文件路径 | 按改动内容(增删的行) | 按评估与方案 |
|---|---|---|---|
| schema | riskRules.schema.paths | riskRules.schema.patterns | dataStructure 标记；方案含迁移 |
| authz | riskRules.authz.paths | riskRules.authz.patterns | 影响类别为权限或数据归属 |
| contract | riskRules.contract.paths；端点处理方法所在文件 | riskRules.contract.patterns | publicContract 标记 |
| protected | boundaries.protected.highRisk | — | — |

规则取值在 settings 的 `controls.implement.design.riskRules`(路径写法同受保护文件，内容为正则)，这里不写任何路径；
端点处理方法所在文件取项目的端点清单(`controls.implement.check.runtime.api.endpoints`，`[{method, route, sourceFile}]`，
与接口浅跑共用)，没有配置时不按它判。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from tightrein.agents.params import HIGH_RISK
from tightrein.implement.context import Risk
from tightrein.protocol.boundaries import high_risk, matching_pattern
from tightrein.settings.load import Settings

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext

POINT = "implement.design"
CATEGORIES = ("schema", "authz", "contract")
FLAG_CATEGORIES = {"dataStructure": "schema", "publicContract": "contract"}
AUTHZ_IMPACTS = frozenset({"authorization", "data_ownership", "data-ownership"})


@dataclass(frozen=True)
class Changed:
    """候选文件(方案前，行为空)或实际改动的文件。"""

    path: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskRules:
    paths: Mapping[str, tuple[str, ...]]
    patterns: Mapping[str, tuple[re.Pattern[str], ...]]

    @classmethod
    def from_settings(cls, settings: Settings) -> RiskRules:
        configured = settings.section(POINT).get("riskRules") or {}
        return cls({name: tuple((configured.get(name) or {}).get("paths", ())) for name in CATEGORIES},
                   {name: _compiled(tuple((configured.get(name) or {}).get("patterns", ()))) for name in CATEGORIES})


def judge(files: Sequence[Changed], *, flags: Mapping[str, bool], impact: str | None, migration: bool,
          settings: Settings, endpoints: Collection[str] = ()) -> Risk:
    """endpoints 为端点处理方法所在的文件(endpoint_files)，改到即为 contract。"""
    rules = RiskRules.from_settings(settings)
    reasons: list[str] = []
    for changed in files:
        if changed.path in endpoints:
            reasons.append(f"contract：{changed.path} 是端点处理方法所在的文件")
        for category in CATEGORIES:
            rule = matching_pattern(changed.path, rules.paths[category])
            if rule is not None:
                reasons.append(f"{category}：{changed.path} 命中路径 {rule}")
            line = next((text for text in (*changed.added, *changed.removed)
                         if any(pattern.search(text) for pattern in rules.patterns[category])), None)
            if line is not None:
                reasons.append(f"{category}：{changed.path} 的改动行命中内容规则：{line.strip()}")
    reasons += [f"{category}：标记 {name}" for name, category in FLAG_CATEGORIES.items() if flags.get(name)]
    if migration:
        reasons.append("schema：含数据库迁移")
    if impact in AUTHZ_IMPACTS:
        reasons.append(f"authz：影响类别 {impact}")
    protected = high_risk((changed.path for changed in files), settings)
    reasons += [f"protected：{path} 是高风险文件" for path in protected]
    return Risk(bool(reasons), tuple(dict.fromkeys(reasons)), tuple(protected))


def plan_risk(context: ImplementContext, settings: Settings) -> Risk:
    """方案前：代码笔记涉及的文件(根因与联动)加评估的标记。"""
    files = context.notes.files if context.notes is not None else []
    return judge([Changed(path) for path in files], flags=_issue_flags(context), impact=_impact(context),
                 migration=False, settings=settings, endpoints=endpoint_files(context, settings))


def design_risk(plan: Mapping[str, Any], context: ImplementContext, settings: Settings) -> Risk:
    """方案通过后：方案的文件、方案与评估的标记、迁移。"""
    paths = [str(item["path"]) for item in plan.get("files") or []]
    return judge([Changed(path) for path in paths], flags=_flags(plan, context), impact=_impact(context),
                 migration=plan.get("migration") is not None, settings=settings,
                 endpoints=endpoint_files(context, settings))


def apply_risk(changes: Iterable[Changed], plan: Mapping[str, Any], context: ImplementContext,
               settings: Settings) -> Risk:
    """自检通过后：相对基准的实际改动(带增删的行)、标记与迁移；审查据此决定是否加深度审查。"""
    return judge(list(changes), flags=_flags(plan, context), impact=_impact(context),
                 migration=plan.get("migration") is not None, settings=settings,
                 endpoints=endpoint_files(context, settings))


def endpoint_files(context: ImplementContext, settings: Settings) -> frozenset[str]:
    """项目端点清单中各端点处理方法所在的文件；没有 worktree 或没有配置清单时为空。"""
    from tightrein.implement.check.runtime.api import ApiSettings, inventory

    if context.worktree is None:
        return frozenset()
    found = inventory(context.worktree, ApiSettings.from_settings(settings).endpoints)
    return frozenset(str(item["sourceFile"]) for item in found if item.get("sourceFile"))


def conditions(risk: Risk | None) -> tuple[str, ...]:
    """调用点按条件选模型：高风险时带 high_risk(路由取值在 settings 的 modelWhen)。"""
    return (HIGH_RISK,) if risk is not None and risk.high else ()


def to_facts(risk: Risk) -> dict[str, Any]:
    return {"high": risk.high, "reasons": list(risk.reasons), "highRiskPaths": list(risk.high_risk_paths)}


def _flags(plan: Mapping[str, Any], context: ImplementContext) -> dict[str, bool]:
    found = _issue_flags(context)
    for name in FLAG_CATEGORIES:
        found[name] = found.get(name, False) or bool(((plan.get("flags") or {}).get(name) or {}).get("flagged"))
    return found


def _issue_flags(context: ImplementContext) -> dict[str, bool]:
    """评估写在 Issue 上的标记(issues.extra.flags：名称 → 是否命中)。"""
    flags = context.issue.extra.get("flags") or {}
    return {name: bool(flags.get(name)) for name in FLAG_CATEGORIES}


def _impact(context: ImplementContext) -> str | None:
    value = context.issue.extra.get("impact")
    return str(value) if value else None


@cache
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern) for pattern in patterns)
