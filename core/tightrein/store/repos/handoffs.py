"""handoffs 表：交接文档索引，键为(环节、验证阶段、对象、次数)。文件由 files/handoff_files.py 写入，本表记录其位置。

phase 只有 verify 使用；其余环节在表中为空串，在记录中为 None。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import HandoffStatus, RunStage, VerifyPhase
from tightrein.store.repos.table import TIME, Table, enum_codec

NO_PHASE = ""


@dataclass(frozen=True)
class HandoffRecord:
    stage: RunStage
    subject_id: str
    attempt: int
    run_id: str
    path: str
    status: HandoffStatus
    schema_version: int
    created_at: datetime
    phase: VerifyPhase | None = None
    stale_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.stage is RunStage.VERIFY) != (self.phase is not None):
            raise ValueError("只有 verify 的交接文档带验证阶段，且 verify 的交接文档必须带验证阶段")
        if self.attempt < 1:
            raise ValueError(f"次数从 1 开始：{self.attempt}")


TABLE = Table(
    "handoffs",
    HandoffRecord,
    ("stage", "phase", "subject_id", "attempt"),
    {
        "stage": enum_codec(RunStage),
        "status": enum_codec(HandoffStatus),
        "created_at": TIME,
        "phase": enum_codec(VerifyPhase, NO_PHASE),
        "stale_at": TIME,
    },
    order_by="created_at, stage, phase, subject_id, attempt",
)


def save(conn: sqlite3.Connection, record: HandoffRecord) -> None:
    TABLE.save(conn, record)


def get(
    conn: sqlite3.Connection,
    stage: RunStage,
    subject_id: str,
    *,
    phase: VerifyPhase | None = None,
    attempt: int | None = None,
) -> HandoffRecord | None:
    """attempt 不给出时取次数最大的一份。"""
    records = TABLE.find(conn, stage=stage, phase=phase, subject_id=subject_id)
    if attempt is not None:
        records = [record for record in records if record.attempt == attempt]
    return max(records, key=lambda record: record.attempt) if records else None


def for_subject(conn: sqlite3.Connection, subject_id: str) -> list[HandoffRecord]:
    return TABLE.find(conn, subject_id=subject_id)


def for_run(conn: sqlite3.Connection, run_id: str) -> list[HandoffRecord]:
    return TABLE.find(conn, run_id=run_id)


def mark_stale(
    conn: sqlite3.Connection, stage: RunStage, subject_id: str, at: datetime, phase: VerifyPhase | None = None
) -> int:
    """把某环节某对象尚未过期的交接文档标记为过期(continue --from 重来时)，返回标记的条数。"""
    cursor = conn.execute(
        "UPDATE handoffs SET stale_at = ? WHERE stage = ? AND phase = ? AND subject_id = ? AND stale_at IS NULL",
        (format_iso(at), stage.value, phase.value if phase is not None else NO_PHASE, subject_id),
    )
    return cursor.rowcount
