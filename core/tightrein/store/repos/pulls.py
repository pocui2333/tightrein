"""pulls 表：PR 跟踪状态，每个 Issue 一行。state 与 mergeable 保存 gh 返回的原值。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.store.repos.table import JSON, TIME, Table, given


@dataclass(frozen=True)
class PullRecord:
    issue_id: str
    number: int
    url: str
    branch: str
    title: str
    state: str
    created_at: datetime
    reviews: list[dict[str, Any]] = field(default_factory=list)
    mergeable: str | None = None
    merge_commit: str | None = None
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    close_note: str | None = None
    last_checked_at: datetime | None = None
    last_reminded_at: datetime | None = None
    master_at: datetime | None = None


TABLE = Table(
    "pulls",
    PullRecord,
    ("issue_id",),
    {"created_at": TIME, "reviews": JSON, "merged_at": TIME, "closed_at": TIME, "last_checked_at": TIME,
     "last_reminded_at": TIME, "master_at": TIME},
    order_by="created_at, issue_id",
)


def save(conn: sqlite3.Connection, pull: PullRecord) -> None:
    TABLE.save(conn, pull)


def get(conn: sqlite3.Connection, issue_id: str) -> PullRecord | None:
    return TABLE.get(conn, issue_id=issue_id)


def find(conn: sqlite3.Connection, *, state: str | None = None) -> list[PullRecord]:
    return TABLE.find(conn, **given({"state": state}))
