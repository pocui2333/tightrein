"""不通过项的分派(architecture/07 4.11)：按性质分组；多种性质并存时按设计问题、规范要求用户确认、计划没覆盖、
局部问题的顺序只处理最靠前的一种，其余一并写入报告。"""

from __future__ import annotations

from collections.abc import Iterable

from tightrein.domain.enums import ReviewCategory
from tightrein.pipeline.fix.steps.checks import Finding

ORDER = (ReviewCategory.DESIGN, ReviewCategory.NEEDS_USER, ReviewCategory.PLAN_GAP, ReviewCategory.LOCAL)


def classify(findings: Iterable[Finding]) -> dict[ReviewCategory, list[Finding]]:
    groups: dict[ReviewCategory, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(finding.category, []).append(finding)
    return {category: groups[category] for category in ORDER if category in groups}


def first(groups: dict[ReviewCategory, list[Finding]]) -> ReviewCategory | None:
    return next(iter(groups), None)
