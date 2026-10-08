"""不通过项与它的性质：自检与审查共用。

性质决定怎样处理，多种并存时只处理最靠前的一类(设计问题 > 需要用户 > 方案缺口 > 局部问题)，其余一并写进报告：
- 局部问题(local)：交回编码，只给这一类的项；
- 方案缺口(plan_gap)：重出方案；
- 需要用户(needs_user)、设计问题(design)：停下交人。

每条都带位置与类型：连续两轮按(位置, 类型)相同即「没有进展」(protocol.limits.no_progress)；交接中写成
`{check, kind, location, summary, category, trigger}`(44c：status、watch 读 location、kind、summary)。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

DESIGN = "design"
NEEDS_USER = "needs_user"
PLAN_GAP = "plan_gap"
LOCAL = "local"
ORDER = (DESIGN, NEEDS_USER, PLAN_GAP, LOCAL)


@dataclass(frozen=True)
class Finding:
    check: str  # 出处：project、rules、runtime、review、review.deep
    kind: str  # 类型：over_cap、outside_plan、root-cause-unfixed …
    location: str | None  # 文件路径:行号、文件路径或接口、页面；整体性的为 None
    summary: str  # 原因(status、watch 显示这一栏)
    category: str
    trigger: str | None = None

    def key(self) -> tuple[str, str]:
        return self.location or "", self.kind

    def text(self) -> str:
        place = f"{self.location}：" if self.location else ""
        trigger = f"(触发条件：{self.trigger})" if self.trigger else ""
        return f"[{self.check}/{self.kind}] {place}{self.summary}{trigger}"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Finding:
        return cls(data["check"], data["kind"], data.get("location"), data["summary"], data["category"],
                   data.get("trigger"))


def first_category(findings: Iterable[Finding]) -> str | None:
    present = {finding.category for finding in findings}
    return next((category for category in ORDER if category in present), None)


def of_first_category(findings: Sequence[Finding]) -> list[Finding]:
    """只留最靠前那一类：交回编码时不把方案缺口之类的项也塞给它。"""
    category = first_category(findings)
    return [finding for finding in findings if finding.category == category]


def keys(findings: Iterable[Finding]) -> list[tuple[str, str]]:
    return sorted(finding.key() for finding in findings)


def from_facts(facts: Mapping[str, Any]) -> list[Finding]:
    return [Finding.from_json(item) for item in facts.get("blockers") or []]
