"""occurrences 表：问题的每次出现(哪次运行、什么时间、哪个 commit、证据)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.protocol.naming import Clock, format_iso
from tightrein.store.tables.table import Table


@dataclass
class Occurrence:
    problem: str
    seen_at: datetime
    source: str
    run: str | None = None
    commit: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    id: int | None = None  # 插入时由数据库生成


TABLE = Table(
    "occurrences", Occurrence,
    times=("seen_at",), json_columns=("evidence",), order_by='"seen_at", "id"', generated=True,
)


def add(conn: sqlite3.Connection, occurrence: Occurrence, clock: Clock) -> int:
    """插入一次出现，返回它的编号。"""
    return TABLE.insert(conn, occurrence, clock.now())


def get(conn: sqlite3.Connection, occurrence_id: int) -> Occurrence | None:
    return TABLE.get(conn, occurrence_id)


def find(conn: sqlite3.Connection, problem: str, *, since: datetime | None = None) -> list[Occurrence]:
    """一个问题的出现记录，按时间排序；since 给出时只取该时间及以后的(验收的观察期)。"""
    if since is None:
        return TABLE.find(conn, problem=problem)
    rows = conn.execute(
        'SELECT * FROM occurrences WHERE problem = ? AND seen_at >= ? ORDER BY "seen_at", "id"',
        (problem, format_iso(since)),
    ).fetchall()
    return [TABLE.from_row(row) for row in rows]
