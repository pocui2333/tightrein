"""workspace_meta 表：工作区级的少量状态(阶段 phase、暂停标记 paused)，键值形式。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.store.repos.table import TIME, Table

PHASE = "phase"
PAUSED = "paused"


@dataclass(frozen=True)
class MetaValue:
    key: str
    value: str
    updated_at: datetime


TABLE = Table("workspace_meta", MetaValue, ("key",), {"updated_at": TIME}, order_by="key")


def get(conn: sqlite3.Connection, key: str) -> str | None:
    found = TABLE.get(conn, key=key)
    return None if found is None else found.value


def set_value(conn: sqlite3.Connection, key: str, value: str, at: datetime) -> None:
    TABLE.save(conn, MetaValue(key, value, at))


def remove(conn: sqlite3.Connection, key: str) -> None:
    TABLE.delete(conn, key=key)
