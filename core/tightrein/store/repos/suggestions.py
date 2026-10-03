"""suggestions 表：学习建议、复核建议与评测用例建议。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.domain.enums import SuggestionKind, SuggestionStatus
from tightrein.store.repos.table import JSON, TIME, Table, enum_codec, given


@dataclass(frozen=True)
class SuggestionRecord:
    id: str
    kind: SuggestionKind
    subject: str
    status: SuggestionStatus
    created_at: datetime
    evidence: dict[str, Any] = field(default_factory=dict)
    target_path: str | None = None
    diff: str | None = None
    base_hash: str | None = None
    reason: str | None = None
    decided_at: datetime | None = None


TABLE = Table(
    "suggestions",
    SuggestionRecord,
    ("id",),
    {"kind": enum_codec(SuggestionKind), "status": enum_codec(SuggestionStatus), "created_at": TIME,
     "evidence": JSON, "decided_at": TIME},
    order_by="created_at, id",
)


def save(conn: sqlite3.Connection, record: SuggestionRecord) -> None:
    TABLE.save(conn, record)


def get(conn: sqlite3.Connection, suggestion_id: str) -> SuggestionRecord | None:
    return TABLE.get(conn, id=suggestion_id)


def find(
    conn: sqlite3.Connection, *, status: SuggestionStatus | None = None, kind: SuggestionKind | None = None
) -> list[SuggestionRecord]:
    return TABLE.find(conn, **given({"status": status, "kind": kind}))
