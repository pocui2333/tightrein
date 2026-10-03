"""search、get、related 的实现(architecture/03 1.6.5)。事件、同步与命中计数由 KnowledgeService 负责。

- search：校验过滤条件与查询串，各来源取 limit × candidateFactor 个候选，一个来源时直接截取，多个时 RRF 融合后截取；
  只返回编号、类型、摘要、路径与得分，不返回正文。
- read_entry：从文件读取 frontmatter 与正文。
- related：四种关系的条目，包含非 active 的条目，便于查看历史；取代链沿 supersededBy 向后追到当前有效的条目。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from tightrein.config import layers
from tightrein.retrieval.errors import EntryNotFound
from tightrein.retrieval.models import EntryDocument, RelatedEntry, Relation, SearchFilters, SearchHit
from tightrein.retrieval.query import match_expression
from tightrein.retrieval.ranking import CandidateSource, rrf_fuse
from tightrein.store.files import markdown
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import knowledge
from tightrein.store.repos.knowledge import KnowledgeRecord

def search(conn: sqlite3.Connection, sources: Sequence[CandidateSource], query: str,
           filters: SearchFilters, *, candidate_factor: int | None = None, rrf_k: int | None = None) -> list[SearchHit]:
    """candidate_factor、rrf_k 缺省取 runtime.retrieval.candidateFactor、rrfK 的核心缺省值。"""
    filters.check()
    match_expression(query)
    factor = int(layers.core_value("runtime.retrieval.candidateFactor")) if candidate_factor is None \
        else candidate_factor
    rankings = [source.candidates(query, filters, filters.limit * factor) for source in sources]
    k = int(layers.core_value("runtime.retrieval.rrfK")) if rrf_k is None else rrf_k
    ranked = rankings[0] if len(rankings) == 1 else rrf_fuse(rankings, k)
    hits = []
    for item in ranked[:filters.limit]:
        record = knowledge.get(conn, item.id)
        if record is not None:
            hits.append(SearchHit.of(record, item.score))
    return hits


def require(conn: sqlite3.Connection, entry_id: str) -> KnowledgeRecord:
    record = knowledge.get(conn, entry_id)
    if record is None:
        raise EntryNotFound(entry_id)
    return record


def read_entry(layout: WorkspaceLayout, record: KnowledgeRecord) -> EntryDocument:
    document = markdown.read(layout.root / record.path)
    return EntryDocument(record.id, document.frontmatter, document.body, record.path)


def related(conn: sqlite3.Connection, entry_id: str) -> list[RelatedEntry]:
    record = require(conn, entry_id)
    records = {item.id: item for item in knowledge.find(conn)}
    found: list[RelatedEntry] = []

    def add(item: KnowledgeRecord, relation: Relation) -> None:
        found.append(RelatedEntry(SearchHit.of(item), relation, item.status))

    for target in record.related:
        if target in records:
            add(records[target], "related")
    for item in records.values():
        if entry_id in item.related:
            add(item, "related-by")
    seen = {entry_id}
    current = record
    while current.superseded_by is not None and current.superseded_by in records \
            and current.superseded_by not in seen:
        current = records[current.superseded_by]
        seen.add(current.id)
        add(current, "superseded-by")
    for item in records.values():
        if item.superseded_by == entry_id:
            add(item, "supersedes")
    return found
