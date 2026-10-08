"""counters 表：预算用量与熔断计数(resources.md、limits.md)。

累加在一条语句内完成，多个进程同时累加不丢数；window_start 记下这一轮计数从何时开始，由调用方按窗口判断是否重置。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.protocol.naming import Clock, format_iso
from tightrein.store.tables.table import Table


@dataclass
class Counter:
    key: str
    value: float
    window_start: datetime
    extra: dict[str, Any] = field(default_factory=dict)


TABLE = Table("counters", Counter, key="key", times=("window_start",), json_columns=("extra",))


def get(conn: sqlite3.Connection, key: str) -> float:
    """当前值；没有计过时为 0。"""
    row = conn.execute('SELECT value FROM counters WHERE "key" = ?', (key,)).fetchone()
    return 0.0 if row is None else float(row[0])


def entry(conn: sqlite3.Connection, key: str) -> Counter | None:
    """整行(含窗口开始时间)；没有计过时为 None。"""
    return TABLE.get(conn, key)


def add(conn: sqlite3.Connection, key: str, amount: float, clock: Clock) -> float:
    """累加并返回累加后的值；第一次累加时窗口从现在开始。"""
    now = format_iso(clock.now())
    row = conn.execute(
        'INSERT INTO counters ("key", value, window_start, created_at, updated_at) VALUES (?, ?, ?, ?, ?) '
        'ON CONFLICT ("key") DO UPDATE SET value = value + excluded.value, updated_at = excluded.updated_at '
        "RETURNING value",
        (key, amount, now, now, now),
    ).fetchone()
    return float(row[0])


def reset(conn: sqlite3.Connection, key: str, clock: Clock) -> None:
    """清零，窗口从现在重新开始。"""
    now = format_iso(clock.now())
    conn.execute(
        'INSERT INTO counters ("key", value, window_start, created_at, updated_at) VALUES (?, 0, ?, ?, ?) '
        'ON CONFLICT ("key") DO UPDATE SET value = 0, window_start = excluded.window_start, '
        "updated_at = excluded.updated_at",
        (key, now, now, now),
    )


def find(conn: sqlite3.Connection, prefix: str) -> dict[str, float]:
    """键以 prefix 开头的全部计数(例如各工具的依赖熔断)。"""
    rows = conn.execute(
        'SELECT "key", value FROM counters WHERE substr("key", 1, ?) = ? ORDER BY "key"', (len(prefix), prefix)
    ).fetchall()
    return {row[0]: float(row[1]) for row in rows}
