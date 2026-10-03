"""source_cursors 表：平台来源按时间窗口增量读取的位置(redesign/01-collect.md 第 1 节)。

source 标识一个来源(采集方法与扩展点，例如 platform-errors:error-tracking)；cursor 至少含上次读到的窗口终点
until，访问日志另存基线；parse_state 为 log-parse 扩展返回的解析状态，核心原样保存、不解读。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tightrein.store.repos.table import JSON, TIME, Table


@dataclass(frozen=True)
class SourceCursor:
    source: str
    cursor: dict[str, Any]
    updated_at: datetime
    parse_state: dict[str, Any] | None = None


TABLE = Table(
    "source_cursors", SourceCursor, ("source",),
    {"cursor": JSON, "parse_state": JSON, "updated_at": TIME}, order_by="source",
)


def save(conn: sqlite3.Connection, cursor: SourceCursor) -> None:
    TABLE.save(conn, cursor)


def get(conn: sqlite3.Connection, source: str) -> SourceCursor | None:
    return TABLE.get(conn, source=source)
