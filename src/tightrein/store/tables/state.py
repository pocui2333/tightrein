"""state 表：键值状态(各来源的读取位置、上次的哈希与时间、接入进度等)，值存 JSON。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from tightrein.protocol.naming import Clock, format_iso
from tightrein.store.tables.table import dumps, upsert


def get(conn: sqlite3.Connection, key: str) -> Any | None:
    row = conn.execute('SELECT value FROM state WHERE "key" = ?', (key,)).fetchone()
    return None if row is None else json.loads(row[0])


def put(conn: sqlite3.Connection, key: str, value: Any, clock: Clock) -> None:
    now = format_iso(clock.now())
    upsert(
        conn, "state", {"key": key, "value": dumps(value), "created_at": now, "updated_at": now}, "key",
        keep=("created_at",),
    )


def delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute('DELETE FROM state WHERE "key" = ?', (key,))


def find(conn: sqlite3.Connection, prefix: str) -> dict[str, Any]:
    """键以 prefix 开头的全部状态(例如某个来源的各项读取位置)。"""
    rows = conn.execute(
        "SELECT \"key\", value FROM state WHERE substr(\"key\", 1, ?) = ? ORDER BY \"key\"", (len(prefix), prefix)
    ).fetchall()
    return {row[0]: json.loads(row[1]) for row in rows}
