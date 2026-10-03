"""KnowledgeEntry 实体(design 16.3)：frontmatter 字段加 path、hits、last_hit_at；后两者只存在于数据库。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.domain.ids import kind_of


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    type: KnowledgeType
    title: str
    summary: str
    status: KnowledgeStatus
    updated: date
    path: str
    tags: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    superseded_by: str | None = None
    review_by: date | None = None
    hits: int = 0
    last_hit_at: datetime | None = None

    def __post_init__(self) -> None:
        if kind_of(self.id) != "knowledge" or not self.id.startswith(f"{self.type.prefix}-"):
            raise ValueError(f"编号 {self.id} 与类型 {self.type.value} 的前缀 {self.type.prefix} 不符")
        if (self.status is KnowledgeStatus.SUPERSEDED) != (self.superseded_by is not None):
            raise ValueError("只有已被取代的条目写明 supersededBy，且已被取代的条目必须写明")
        if self.last_hit_at is not None and self.last_hit_at.tzinfo is None:
            raise ValueError("last_hit_at 必须带时区")

    def needs_review(self, today: date) -> bool:
        """过了复核日期需要确认是否仍然有效。"""
        return self.review_by is not None and today > self.review_by
