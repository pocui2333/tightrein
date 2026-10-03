"""breaker_counts 表：无人值守推进中各对象的连续失败次数与无进展次数(熔断，orchestrator/breaker.py)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.store.repos.table import TIME, Table


@dataclass(frozen=True)
class BreakerCount:
    subject_id: str
    updated_at: datetime
    step: str | None = None
    failures: int = 0
    repeats: int = 0
    last_reason: str | None = None
    tripped_at: datetime | None = None


TABLE = Table("breaker_counts", BreakerCount, ("subject_id",), {"updated_at": TIME, "tripped_at": TIME},
              order_by="subject_id")


def get(conn: sqlite3.Connection, subject_id: str) -> BreakerCount | None:
    return TABLE.get(conn, subject_id=subject_id)


def save(conn: sqlite3.Connection, count: BreakerCount) -> None:
    TABLE.save(conn, count)


def tripped(conn: sqlite3.Connection) -> list[BreakerCount]:
    return [item for item in TABLE.find(conn) if item.tripped_at is not None]


def clear(conn: sqlite3.Connection, subject_id: str) -> None:
    TABLE.delete(conn, subject_id=subject_id)
