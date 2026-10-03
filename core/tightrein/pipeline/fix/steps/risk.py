"""风险判定(architecture/07 4.5，design 5.8)：收集候选文件或实际改动、分诊与计划的标记，调用 domain.fix.risk。

第一次在出计划前，对根因文件与 fix-scout 的联动文件判定，作为出计划的输入；第二次在确定性检查通过后，对相对
基准 commit 的实际改动与计划的标记、迁移判定，决定评审深度。端点处理方法所在的文件取自 authz-endpoints 的输出。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import RiskCategory
from tightrein.domain.fix import ChangedLines, FixRisk, RiskRules, risk
from tightrein.guards.diff_rules import ChangedFile
from tightrein.guards.protected import contains, matches_path
from tightrein.pipeline.fix.steps import scout
from tightrein.pipeline.fix.steps.context import FixContext

PLAN_FLAGS = ("dataStructure", "publicContract")


def rules(config: ProjectConfig) -> RiskRules:
    configured = config.get("review.riskRules")
    return RiskRules(
        paths={category: tuple((configured.get(category.value) or {}).get("paths", ())) for category in RiskCategory},
        patterns={category: tuple((configured.get(category.value) or {}).get("patterns", ()))
                  for category in RiskCategory},
        categories=frozenset(RiskCategory(item) for item in config.get("review.deepTriggers.categories")),
    )


def endpoint_files(output: Mapping[str, Any] | None) -> set[str]:
    return {item["sourceFile"] for item in (output or {}).get("endpoints", []) if item.get("sourceFile")}


def _judge(files: Sequence[ChangedLines], flags: Mapping[str, bool], context: FixContext, config: ProjectConfig,
           endpoints: Collection[str], migration: bool) -> FixRisk:
    return risk(files, flags, context.impact_kind, endpoints, rules(config), path_matches=matches_path,
                line_contains=contains, migration=migration)


def plan_risk(context: FixContext, scouting: Mapping[str, Any] | None, config: ProjectConfig,
              endpoints: Collection[str]) -> FixRisk:
    files = dict.fromkeys([*context.root_files, *scout.linkage_files(scouting)])
    return _judge([ChangedLines(path) for path in files], context.flags, context, config, endpoints, False)


def apply_risk(changes: Sequence[ChangedFile], plan: Mapping[str, Any], context: FixContext, config: ProjectConfig,
               endpoints: Collection[str]) -> FixRisk:
    plan_flags = plan.get("flags") or {}
    flags = {name: context.flags.get(name, False) or bool((plan_flags.get(name) or {}).get("flagged"))
             for name in PLAN_FLAGS}
    files = [ChangedLines(item.path, item.added, item.removed) for item in changes]
    return _judge(files, flags, context, config, endpoints, plan.get("migration") is not None)
