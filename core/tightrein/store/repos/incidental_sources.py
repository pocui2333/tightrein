"""incidental_sources 表：任务外发现的已读记录，每个来源文件一行。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.store.repos.table import TIME, Table


@dataclass(frozen=True)
class IncidentalSource:
    source_path: str
    content_hash: str
    read_at: datetime
    signal_count: int


TABLE = Table("incidental_sources", IncidentalSource, ("source_path",), {"read_at": TIME}, order_by="source_path")


def save(conn: sqlite3.Connection, source: IncidentalSource) -> None:
    TABLE.save(conn, source)


def get(conn: sqlite3.Connection, source_path: str) -> IncidentalSource | None:
    return TABLE.get(conn, source_path=source_path)
