"""sequences 表：编号分配，每个序列一行。

递增在一条语句内完成，多个进程同时分配也不重号；在外层事务中分配的随事务回滚一并撤销。
"""

from __future__ import annotations

import sqlite3

PROBLEM = "problem"
ISSUE = "issue"

_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


def next_value(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "INSERT INTO sequences (name, value) VALUES (?, 1) "
        f"ON CONFLICT (name) DO UPDATE SET value = value + 1, updated_at = {_NOW} RETURNING value",
        (name,),
    ).fetchone()
    return int(row[0])


def current(conn: sqlite3.Connection, name: str) -> int:
    """最近分配的值；还没有分配过时为 0。"""
    row = conn.execute("SELECT value FROM sequences WHERE name = ?", (name,)).fetchone()
    return 0 if row is None else int(row[0])


def ensure_at_least(conn: sqlite3.Connection, name: str, value: int) -> None:
    """保证下一次分配大于 value：从文件重建后，不再发出已被文件占用的编号。只前进，不后退。"""
    conn.execute(
        "INSERT INTO sequences (name, value) VALUES (?, ?) "
        f"ON CONFLICT (name) DO UPDATE SET value = MAX(value, excluded.value), updated_at = {_NOW}",
        (name, value),
    )
