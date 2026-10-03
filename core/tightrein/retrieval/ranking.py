"""候选来源与融合(architecture/03 1.4、1.10)。

search 遍历已配置的全部 CandidateSource，每个来源各取 limit × runtime.retrieval.candidateFactor 个候选；只有一个来源时按它的顺序截取，多于一个时
用 rrf_fuse 合并(常数为 runtime.retrieval.rrfK)。引入向量检索时新增 VectorSource，search、context_for 与两个入口不需要改动。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from tightrein.retrieval.models import SearchFilters
from tightrein.config import layers
from tightrein.retrieval.query import match_expression, rank_expression
from tightrein.store.repos import knowledge

@dataclass(frozen=True)
class RankedId:
    id: str
    score: float


class CandidateSource(Protocol):
    name: str

    def candidates(self, query: str, filters: SearchFilters, limit: int) -> list[RankedId]: ...


class FtsSource:
    """FTS5 全文检索；score 为 bm25 值的相反数，越大越相关。"""

    name = "fts"

    def __init__(self, conn: sqlite3.Connection, weights: Mapping[str, float] | None = None) -> None:
        """weights 为各列的 bm25 权重，缺省取 runtime.retrieval.columnWeights 的核心缺省值。"""
        self.conn = conn
        self.rank = rank_expression(weights if weights is not None else layers.core_value(
            "runtime.retrieval.columnWeights"))

    def candidates(self, query: str, filters: SearchFilters, limit: int) -> list[RankedId]:
        rows = knowledge.match(self.conn, match_expression(query), self.rank, limit=limit, types=filters.types,
                               tags=filters.tags, status=filters.status)
        return [RankedId(entry_id, -rank) for entry_id, rank in rows]


def rrf_fuse(rankings: Sequence[Sequence[RankedId]], k: int) -> list[RankedId]:
    """倒数排名融合：得分为各来源中 1 / (k + 排名) 之和，排名从 1 开始；得分相同时按编号。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            scores[item.id] = scores.get(item.id, 0.0) + 1.0 / (k + position)
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    return [RankedId(entry_id, score) for entry_id, score in ordered]
