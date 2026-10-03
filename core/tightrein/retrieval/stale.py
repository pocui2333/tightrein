"""待复核的条目(architecture/03 1.6.5，design 16.7)，只看 active 的条目，不含文档：

- 过期：reviewBy 早于今天；
- 未命中：最近命中早于当前时间减去 unusedDays，从未命中的以 updated 代替；
- 矛盾候选组：同一类型内，标签交集不少于 2 个的条目按连通关系分组；超过 8 条的组按编号均匀拆成若干组，
  每组 2 到 8 条。是否矛盾由 learn 经执行器比对后决定。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from tightrein.config import layers
from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.retrieval.models import SearchHit, StaleReport
from tightrein.store.repos import knowledge
from tightrein.store.repos.knowledge import ENTRY_TYPES, KnowledgeRecord



def _components(records: Sequence[KnowledgeRecord], min_shared_tags: int) -> list[list[KnowledgeRecord]]:
    parent = {record.id: record.id for record in records}

    def root(entry_id: str) -> str:
        while parent[entry_id] != entry_id:
            parent[entry_id] = parent[parent[entry_id]]
            entry_id = parent[entry_id]
        return entry_id

    for index, first in enumerate(records):
        for second in records[index + 1:]:
            if len(set(first.tags) & set(second.tags)) >= min_shared_tags:
                parent[root(second.id)] = root(first.id)
    groups: dict[str, list[KnowledgeRecord]] = {}
    for record in records:
        groups.setdefault(root(record.id), []).append(record)
    return sorted((group for group in groups.values() if len(group) > 1), key=lambda group: group[0].id)


def _split(group: list[KnowledgeRecord], max_size: int) -> list[list[KnowledgeRecord]]:
    """超过上限的组拆成 ceil(n / max_size) 组，各组大小相差不超过 1，因此每组至少 2 条。"""
    count = -(-len(group) // max_size)
    size, extra = divmod(len(group), count)
    parts, start = [], 0
    for index in range(count):
        end = start + size + (1 if index < extra else 0)
        parts.append(group[start:end])
        start = end
    return parts


def stale(conn: sqlite3.Connection, now: datetime, today: date, unused_days: int, *,
          min_shared_tags: int | None = None, max_group_size: int | None = None) -> StaleReport:
    """min_shared_tags、max_group_size 缺省取 runtime.retrieval.staleMinSharedTags、staleMaxGroupSize 的核心缺省值。"""
    shared = int(layers.core_value("runtime.retrieval.staleMinSharedTags")) if min_shared_tags is None \
        else min_shared_tags
    size = int(layers.core_value("runtime.retrieval.staleMaxGroupSize")) if max_group_size is None else max_group_size
    entries = knowledge.find(conn, types=ENTRY_TYPES, status=KnowledgeStatus.ACTIVE)
    overdue = [SearchHit.of(record) for record in entries if record.review_by is not None and record.review_by < today]
    cutoff = now - timedelta(days=unused_days)
    unused = [
        SearchHit.of(record) for record in entries
        if (record.last_hit_at < cutoff if record.last_hit_at is not None
            else record.updated < today - timedelta(days=unused_days))
    ]
    groups: list[list[SearchHit]] = []
    for kind in KnowledgeType:
        same_type = [record for record in entries if record.type == kind.value]
        for component in _components(same_type, shared):
            groups += [[SearchHit.of(record) for record in part] for part in _split(component, size)]
    return StaleReport(overdue, unused, groups)
