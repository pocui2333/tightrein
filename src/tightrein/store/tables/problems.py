"""problems 表：每个问题一行(去重后的结果)。

状态取值与转换由采集的去重与评估的状态机管(44c)，这里只存取。别名、关联、回归等偶尔用到的放在 extra。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.protocol.naming import Clock
from tightrein.store.tables.table import Table


@dataclass
class Problem:
    id: str
    fingerprint: str
    source: str
    check_type: str
    status: str
    title: str
    first_seen: datetime
    last_seen: datetime
    location: str | None = None
    count: int = 1
    last_commit: str | None = None
    issue: str | None = None
    muted_until: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


TABLE = Table(
    "problems", Problem,
    times=("first_seen", "last_seen", "muted_until"), json_columns=("extra",), order_by="id",
)


def get(conn: sqlite3.Connection, problem_id: str) -> Problem | None:
    return TABLE.get(conn, problem_id)


def by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> Problem | None:
    """同一指纹有多条时取编号最大的(最近建立的)。"""
    matches = TABLE.find(conn, fingerprint=fingerprint)
    return matches[-1] if matches else None


def find(conn: sqlite3.Connection, *, status: str | None = None, issue: str | None = None) -> list[Problem]:
    return TABLE.find(conn, status=status, issue=issue)


def save(conn: sqlite3.Connection, problem: Problem, clock: Clock) -> None:
    TABLE.save(conn, problem, clock.now())
