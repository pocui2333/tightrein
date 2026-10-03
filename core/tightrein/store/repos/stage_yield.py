"""stage_yield 表：环节效益(design 14.2)。登记时 outcome 为 pending(写入去重等登记时即可判定的除外)，由 learn 回填；
tool、model 为这次调用实际使用的工具与模型(迁移 012 之前的行为空)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import RunnerStatus, Stage, YieldOutcome
from tightrein.store.repos.table import TIME, Table, enum_codec


@dataclass(frozen=True)
class StageYieldRecord:
    run_id: str
    stage: Stage
    role: str
    subject_id: str
    attempt: int
    runner_status: RunnerStatus
    created_at: datetime
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    outcome: YieldOutcome = YieldOutcome.PENDING
    outcome_reason: str | None = None
    decided_at: datetime | None = None
    tool: str | None = None
    model: str | None = None
    id: int | None = None


TABLE = Table(
    "stage_yield",
    StageYieldRecord,
    ("id",),
    {"stage": enum_codec(Stage), "runner_status": enum_codec(RunnerStatus), "outcome": enum_codec(YieldOutcome),
     "created_at": TIME, "decided_at": TIME},
    order_by="created_at, id",
    generated=("id",),
)


def append(conn: sqlite3.Connection, record: StageYieldRecord) -> int:
    if record.id is not None:
        raise ValueError("登记的记录不能自带编号")
    return TABLE.insert(conn, record)


def find(conn: sqlite3.Connection, *, stage: Stage | None = None, outcome: YieldOutcome | None = None,
         since: datetime | None = None) -> list[StageYieldRecord]:
    """按登记时间升序；since 给出时只取该时间及以后登记的。"""
    filters = {name: value for name, value in (("stage", stage), ("outcome", outcome)) if value is not None}
    records = TABLE.find(conn, **filters)
    return [record for record in records if since is None or record.created_at >= since]


def decide(conn: sqlite3.Connection, record_id: int, outcome: YieldOutcome, reason: str, at: datetime) -> None:
    conn.execute("UPDATE stage_yield SET outcome = ?, outcome_reason = ?, decided_at = ? WHERE id = ?",
                 (outcome.value, reason, format_iso(at), record_id))
