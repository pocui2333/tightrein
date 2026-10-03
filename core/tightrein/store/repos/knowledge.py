"""knowledge_meta 与 knowledge_fts 两张表：知识条目与可检索文档的元数据、命中次数，以及全文索引(architecture/03 1.5)。

条目的 type 为 KnowledgeType 的取值，可与 KnowledgeEntry 互转；Issue、发现报告、修复报告、改进提案四类文档的
status 固定为 active。hits 与 last_hit_at 只存在于数据库：重新写入元数据时保留原值，只由 record_hit 更新。
写入 knowledge_fts 的文本由调用方预处理(architecture/03 1.6.1)，本模块原样写入。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.domain.knowledge import KnowledgeEntry
from tightrein.store.db import transaction
from tightrein.store.repos.table import DATE, STRINGS, TIME, Table, enum_codec, given, upsert

ENTRY_TYPES = tuple(member.value for member in KnowledgeType)
DOCUMENT_TYPES = ("issue", "finding", "fix-report")


@dataclass(frozen=True)
class KnowledgeRecord:
    id: str
    type: str
    status: KnowledgeStatus
    title: str
    summary: str
    updated: date
    path: str
    content_sha256: str
    file_mtime: float
    file_size: int
    indexed_at: datetime
    tags: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    superseded_by: str | None = None
    review_by: date | None = None
    hits: int = 0
    last_hit_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.type not in ENTRY_TYPES + DOCUMENT_TYPES:
            raise ValueError(f"未知的知识类型：{self.type}")
        if self.type in DOCUMENT_TYPES and self.status is not KnowledgeStatus.ACTIVE:
            raise ValueError(f"文档类记录的状态固定为 active：{self.id}")

    @property
    def is_entry(self) -> bool:
        return self.type in ENTRY_TYPES

    def to_entry(self) -> KnowledgeEntry:
        if not self.is_entry:
            raise ValueError(f"{self.id} 是 {self.type} 文档，不是知识条目")
        return KnowledgeEntry(
            id=self.id, type=KnowledgeType(self.type), title=self.title, summary=self.summary, status=self.status,
            updated=self.updated, path=self.path, tags=self.tags, related=self.related,
            superseded_by=self.superseded_by, review_by=self.review_by, hits=self.hits, last_hit_at=self.last_hit_at,
        )

    @classmethod
    def from_entry(
        cls, entry: KnowledgeEntry, content_sha256: str, file_mtime: float, file_size: int, indexed_at: datetime
    ) -> "KnowledgeRecord":
        return cls(
            id=entry.id, type=entry.type.value, status=entry.status, title=entry.title, summary=entry.summary,
            updated=entry.updated, path=entry.path, content_sha256=content_sha256, file_mtime=file_mtime,
            file_size=file_size, indexed_at=indexed_at, tags=entry.tags, related=entry.related,
            superseded_by=entry.superseded_by, review_by=entry.review_by, hits=entry.hits,
            last_hit_at=entry.last_hit_at,
        )


TABLE = Table(
    "knowledge_meta",
    KnowledgeRecord,
    ("id",),
    {"status": enum_codec(KnowledgeStatus), "updated": DATE, "indexed_at": TIME, "tags": STRINGS,
     "related": STRINGS, "review_by": DATE, "last_hit_at": TIME},
    order_by="id",
)
_HIT_COLUMNS = ("hits", "last_hit_at")


def save(conn: sqlite3.Connection, record: KnowledgeRecord) -> None:
    upsert(conn, TABLE.name, TABLE.to_row(record), TABLE.keys, keep=_HIT_COLUMNS)


def get(conn: sqlite3.Connection, knowledge_id: str) -> KnowledgeRecord | None:
    return TABLE.get(conn, id=knowledge_id)


def by_path(conn: sqlite3.Connection, path: str) -> KnowledgeRecord | None:
    return TABLE.get(conn, path=path)


def find(
    conn: sqlite3.Connection, *, types: Iterable[str] | None = None, status: KnowledgeStatus | None = None
) -> list[KnowledgeRecord]:
    """按状态等值过滤、按类型取其一，按编号升序。"""
    records = TABLE.find(conn, **given({"status": status}))
    if types is None:
        return records
    wanted = set(types)
    return [record for record in records if record.type in wanted]


def record_hit(conn: sqlite3.Connection, knowledge_id: str, at: datetime) -> None:
    conn.execute(
        "UPDATE knowledge_meta SET hits = hits + 1, last_hit_at = ? WHERE id = ?", (format_iso(at), knowledge_id)
    )


def save_text(conn: sqlite3.Connection, knowledge_id: str, title: str, summary: str, tags: str, body: str) -> None:
    """替换一条记录的全文索引内容。"""
    with transaction(conn):
        conn.execute("DELETE FROM knowledge_fts WHERE id = ?", (knowledge_id,))
        conn.execute(
            "INSERT INTO knowledge_fts (id, title, summary, tags, body) VALUES (?, ?, ?, ?, ?)",
            (knowledge_id, title, summary, tags, body),
        )


def remove(conn: sqlite3.Connection, knowledge_id: str) -> None:
    """删除元数据与全文索引；只用于文件已被删除时的同步。"""
    with transaction(conn):
        conn.execute("DELETE FROM knowledge_fts WHERE id = ?", (knowledge_id,))
        conn.execute("DELETE FROM knowledge_meta WHERE id = ?", (knowledge_id,))


def remove_text(conn: sqlite3.Connection, knowledge_id: str) -> None:
    """只删除全文索引中的一行。"""
    conn.execute("DELETE FROM knowledge_fts WHERE id = ?", (knowledge_id,))


def match(
    conn: sqlite3.Connection, expression: str, rank: str, *, limit: int, types: Iterable[str] = (),
    tags: Iterable[str] = (), status: KnowledgeStatus | None = None,
) -> list[tuple[str, float]]:
    """全文检索，返回 (编号, 排序值)，排序值越小越相关，相同时按编号。

    expression 为 FTS5 的 MATCH 表达式；rank 为代码中定义的 bm25 排序表达式，不来自外部输入。
    类型取其一，标签必须全部包含(json_each 判断)，状态为空时不限。
    """
    clauses = ["knowledge_fts MATCH ?"]
    params: list[object] = [expression]
    wanted_types = list(types)
    if wanted_types:
        clauses.append(f"m.type IN ({', '.join('?' for _ in wanted_types)})")
        params += wanted_types
    if status is not None:
        clauses.append("m.status = ?")
        params.append(status.value)
    for tag in tags:
        clauses.append("EXISTS (SELECT 1 FROM json_each(m.tags) WHERE json_each.value = ?)")
        params.append(tag)
    params.append(limit)
    rows = conn.execute(
        f"SELECT knowledge_fts.id AS id, {rank} AS rank FROM knowledge_fts "
        f"JOIN knowledge_meta AS m ON m.id = knowledge_fts.id WHERE {' AND '.join(clauses)} "
        "ORDER BY rank, m.id LIMIT ?",
        params,
    ).fetchall()
    return [(row["id"], float(row["rank"])) for row in rows]
