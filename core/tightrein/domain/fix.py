"""修复的风险判定(design 5.8)：任一条命中即为高风险，否则为常规。

| 类别 | 按改动文件 | 按改动内容(新增与删除的行) | 按分诊与计划 |
|---|---|---|---|
| schema | 路径匹配 schema.paths | 匹配 schema.patterns | 「数据结构或存量数据」标记；计划的 migration 非空 |
| authz | 路径匹配 authz.paths | 匹配 authz.patterns | 影响类别为权限或数据归属 |
| contract | 路径匹配 contract.paths；改动落在端点处理方法所在文件 | 匹配 contract.patterns | 「公共实现或接口契约」标记 |

规则的路径与模式由调用方给出(配置 review.riskRules)，路径与内容的匹配方式也由调用方给出(与受保护文件相同)，
本模块不含任何路径、模式与匹配实现；不在 categories 中的类别不参与判定。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tightrein.domain.enums import FixRiskLevel, ImpactKind, RiskCategory

PATH = "path"
CONTENT = "content"
FLAG = "flag"
MIGRATION = "migration"
IMPACT = "impact"
ENDPOINT = "endpoint"
FLAG_CATEGORIES = {"dataStructure": RiskCategory.SCHEMA, "publicContract": RiskCategory.CONTRACT}
AUTHZ_IMPACTS = frozenset({ImpactKind.AUTHORIZATION, ImpactKind.DATA_OWNERSHIP})

PathMatcher = Callable[[str, str], bool]
LineMatcher = Callable[[str, str], bool]


@dataclass(frozen=True)
class RiskRules:
    paths: Mapping[RiskCategory, tuple[str, ...]] = field(default_factory=dict)
    patterns: Mapping[RiskCategory, tuple[str, ...]] = field(default_factory=dict)
    categories: frozenset[RiskCategory] = frozenset(RiskCategory)


@dataclass(frozen=True)
class ChangedLines:
    path: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskHit:
    category: RiskCategory
    basis: str
    rule: str
    file: str | None = None
    excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category.value, "basis": self.basis, "file": self.file, "excerpt": self.excerpt,
                "rule": self.rule}


@dataclass(frozen=True)
class FixRisk:
    level: FixRiskLevel
    hits: tuple[RiskHit, ...] = ()

    @property
    def categories(self) -> tuple[RiskCategory, ...]:
        return tuple(dict.fromkeys(hit.category for hit in self.hits))

    @property
    def high(self) -> bool:
        return self.level is FixRiskLevel.HIGH

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level.value, "categories": [item.value for item in self.categories],
                "hits": [hit.to_dict() for hit in self.hits]}


def _file_hits(files: Sequence[ChangedLines], rules: RiskRules, path_matches: PathMatcher,
               line_contains: LineMatcher) -> list[RiskHit]:
    hits = []
    for changed in files:
        for category in RiskCategory:
            path_rule = next((rule for rule in rules.paths.get(category, ()) if path_matches(changed.path, rule)), None)
            if path_rule is not None:
                hits.append(RiskHit(category, PATH, path_rule, changed.path))
            for line in (*changed.added, *changed.removed):
                pattern = next((rule for rule in rules.patterns.get(category, ()) if line_contains(line, rule)), None)
                if pattern is not None:
                    hits.append(RiskHit(category, CONTENT, pattern, changed.path, line.strip()))
                    break
    return hits


def risk(files: Sequence[ChangedLines], flags: Mapping[str, bool], impact_kind: ImpactKind | None,
         endpoint_files: Collection[str], rules: RiskRules, *, path_matches: PathMatcher, line_contains: LineMatcher,
         migration: bool = False) -> FixRisk:
    """files 为候选文件(出计划前，行为空)或实际改动；flags 的键为 dataStructure、publicContract。"""
    hits = _file_hits(files, rules, path_matches, line_contains)
    for name, category in FLAG_CATEGORIES.items():
        if flags.get(name):
            hits.append(RiskHit(category, FLAG, f"标记 {name}"))
    if migration:
        hits.append(RiskHit(RiskCategory.SCHEMA, MIGRATION, "计划含迁移条目"))
    if impact_kind in AUTHZ_IMPACTS:
        hits.append(RiskHit(RiskCategory.AUTHZ, IMPACT, f"影响类别 {impact_kind.value}"))
    for changed in files:
        if changed.path in endpoint_files:
            hits.append(RiskHit(RiskCategory.CONTRACT, ENDPOINT, "端点处理方法所在文件", changed.path))
    kept = tuple(hit for hit in hits if hit.category in rules.categories)
    return FixRisk(FixRiskLevel.HIGH if kept else FixRiskLevel.NORMAL, kept)
