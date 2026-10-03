"""retrieval 对外的数据结构(architecture/03 1.4)，命令行的 --json 输出与 MCP 工具的结构化结果由 to_dict 给出。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from tightrein.domain.enums import KnowledgeStatus
from tightrein.retrieval.errors import InvalidQuery
from tightrein.store.repos.knowledge import DOCUMENT_TYPES, ENTRY_TYPES, KnowledgeRecord

MIN_LIMIT = 1
MAX_LIMIT = 50
DEFAULT_LIMIT = 10
ANY_STATUS = "any"

Relation = Literal["related", "related-by", "superseded-by", "supersedes"]


@dataclass(frozen=True)
class SearchFilters:
    types: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    status: KnowledgeStatus | None = KnowledgeStatus.ACTIVE
    limit: int = DEFAULT_LIMIT

    def check(self) -> None:
        unknown = [kind for kind in self.types if kind not in ENTRY_TYPES + DOCUMENT_TYPES]
        if unknown:
            raise InvalidQuery(f"不认识的类型：{'、'.join(unknown)}；可选 {'、'.join(ENTRY_TYPES + DOCUMENT_TYPES)}")
        if any(not tag for tag in self.tags):
            raise InvalidQuery("标签不能为空串")
        if not MIN_LIMIT <= self.limit <= MAX_LIMIT:
            raise InvalidQuery(f"limit 须在 {MIN_LIMIT} 到 {MAX_LIMIT} 之间：{self.limit}")

    @classmethod
    def parse(cls, types: tuple[str, ...] = (), tags: tuple[str, ...] = (), status: str = "active",
              limit: int = DEFAULT_LIMIT) -> SearchFilters:
        """由命令行与 MCP 的参数构造；status 取 active、superseded、archived、any。"""
        if status == ANY_STATUS:
            chosen = None
        else:
            try:
                chosen = KnowledgeStatus(status)
            except ValueError:
                allowed = "、".join([*(member.value for member in KnowledgeStatus), ANY_STATUS])
                raise InvalidQuery(f"status 只能是 {allowed}：{status}") from None
        filters = cls(tuple(types), tuple(tags), chosen, limit)
        filters.check()
        return filters

    def to_dict(self) -> dict[str, Any]:
        return {"types": list(self.types), "tags": list(self.tags),
                "status": ANY_STATUS if self.status is None else self.status.value, "limit": self.limit}


@dataclass(frozen=True)
class SearchHit:
    id: str
    type: str
    summary: str
    path: str
    score: float

    @classmethod
    def of(cls, record: KnowledgeRecord, score: float = 0.0) -> SearchHit:
        return cls(record.id, record.type, record.summary, record.path, score)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "summary": self.summary, "path": self.path, "score": self.score}


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    index_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"hits": [hit.to_dict() for hit in self.hits], "indexWarnings": list(self.index_warnings)}


@dataclass(frozen=True)
class EntryDocument:
    id: str
    frontmatter: Mapping[str, Any]
    body: str
    path: str
    index_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "frontmatter": dict(self.frontmatter), "body": self.body, "path": self.path,
                "indexWarnings": list(self.index_warnings)}


@dataclass(frozen=True)
class RelatedEntry:
    hit: SearchHit
    relation: Relation
    status: KnowledgeStatus

    def to_dict(self) -> dict[str, Any]:
        return {**self.hit.to_dict(), "relation": self.relation, "status": self.status.value}


@dataclass(frozen=True)
class RelatedResult:
    entries: list[RelatedEntry]
    index_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [entry.to_dict() for entry in self.entries], "indexWarnings": list(self.index_warnings)}


@dataclass(frozen=True)
class StaleReport:
    overdue: list[SearchHit]
    unused: list[SearchHit]
    contradiction_groups: list[list[SearchHit]]
    index_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"overdue": [hit.to_dict() for hit in self.overdue], "unused": [hit.to_dict() for hit in self.unused],
                "contradictionGroups": [[hit.to_dict() for hit in group] for group in self.contradiction_groups],
                "indexWarnings": list(self.index_warnings)}
