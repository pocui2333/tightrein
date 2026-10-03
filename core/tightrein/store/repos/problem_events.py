"""problem_events 表：问题的状态变化与人工操作。每次转换(包括状态不变的)都写一条，只追加，不修改已有内容。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import ProblemEvent, ProblemStatus
from tightrein.store.repos.table import JSON, TIME, Table, enum_codec

OPERATION_AUTO = "auto"
OPERATION_USER = "user_action"
OPERATIONS = (OPERATION_AUTO, OPERATION_USER)


@dataclass(frozen=True)
class ProblemEventRecord:
    problem_id: str
    at: datetime
    event: ProblemEvent
    operation: str
    from_status: ProblemStatus | None = None
    to_status: ProblemStatus | None = None
    run_id: str | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    handled_at: datetime | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        if self.operation not in OPERATIONS:
            raise ValueError(f"operation 只能是 {'、'.join(OPERATIONS)}：{self.operation}")


TABLE = Table(
    "problem_events",
    ProblemEventRecord,
    ("id",),
    {
        "at": TIME,
        "event": enum_codec(ProblemEvent),
        "from_status": enum_codec(ProblemStatus),
        "to_status": enum_codec(ProblemStatus),
        "detail": JSON,
        "handled_at": TIME,
    },
    order_by="at, id",
    generated=("id",),
)


def append(conn: sqlite3.Connection, record: ProblemEventRecord) -> int:
    """追加一条事件，返回编号。"""
    if record.id is not None:
        raise ValueError("追加的事件不能自带编号")
    return TABLE.insert(conn, record)


def for_problem(conn: sqlite3.Connection, problem_id: str) -> list[ProblemEventRecord]:
    return TABLE.find(conn, problem_id=problem_id)


def unhandled(conn: sqlite3.Connection, event: ProblemEvent) -> list[ProblemEventRecord]:
    """尚未处理的某类事件，例如等待分诊处理的 retriage-requested。"""
    return TABLE.find(conn, event=event, handled_at=None)


def mark_handled(conn: sqlite3.Connection, event_id: int, at: datetime) -> None:
    conn.execute("UPDATE problem_events SET handled_at = ? WHERE id = ?", (format_iso(at), event_id))
