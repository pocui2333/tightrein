"""schedule_state 表：各定时任务最近一次执行的时间与结果。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.enums import RunStatus
from tightrein.store.repos.table import TIME, Table, enum_codec


@dataclass(frozen=True)
class ScheduleState:
    task: str
    missed_count: int = 0
    last_started_at: datetime | None = None
    last_ended_at: datetime | None = None
    last_status: RunStatus | None = None
    last_run_id: str | None = None


TABLE = Table(
    "schedule_state",
    ScheduleState,
    ("task",),
    {"last_started_at": TIME, "last_ended_at": TIME, "last_status": enum_codec(RunStatus)},
    order_by="task",
)


def save(conn: sqlite3.Connection, state: ScheduleState) -> None:
    TABLE.save(conn, state)


def get(conn: sqlite3.Connection, task: str) -> ScheduleState | None:
    return TABLE.get(conn, task=task)


def all_states(conn: sqlite3.Connection) -> list[ScheduleState]:
    return TABLE.find(conn)
