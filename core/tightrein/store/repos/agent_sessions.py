"""agent_sessions 表：交互会话，键为(环节、角色、对象、开始时间)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.enums import AgentSessionStatus, Stage
from tightrein.store.repos.table import TIME, Table, enum_codec, given


@dataclass(frozen=True)
class AgentSession:
    stage: Stage
    role: str
    subject_id: str
    started_at: datetime
    tool: str
    workdir: str
    status: AgentSessionStatus
    session_id: str | None = None
    ended_at: datetime | None = None


TABLE = Table(
    "agent_sessions",
    AgentSession,
    ("stage", "role", "subject_id", "started_at"),
    {"stage": enum_codec(Stage), "started_at": TIME, "status": enum_codec(AgentSessionStatus), "ended_at": TIME},
    order_by="started_at, stage, role, subject_id",
)


def save(conn: sqlite3.Connection, session: AgentSession) -> None:
    TABLE.save(conn, session)


def find(
    conn: sqlite3.Connection,
    *,
    stage: Stage | None = None,
    role: str | None = None,
    subject_id: str | None = None,
    status: AgentSessionStatus | None = None,
) -> list[AgentSession]:
    return TABLE.find(conn, **given({"stage": stage, "role": role, "subject_id": subject_id, "status": status}))


def latest(conn: sqlite3.Connection, stage: Stage, role: str, subject_id: str) -> AgentSession | None:
    sessions = find(conn, stage=stage, role=role, subject_id=subject_id)
    return sessions[-1] if sessions else None
