"""scores 表：评分记录(design 12.3)，只追加。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.enums import ScoreMethod, ScoreResult, Stage
from tightrein.store.repos.table import TIME, Table, enum_codec, given


@dataclass(frozen=True)
class ScoreRecord:
    stage: Stage
    run_id: str
    subject_id: str
    attempt: int
    item: str
    result: ScoreResult
    method: ScoreMethod
    created_at: datetime
    detail: str | None = None
    id: int | None = None


TABLE = Table(
    "scores",
    ScoreRecord,
    ("id",),
    {"stage": enum_codec(Stage), "result": enum_codec(ScoreResult), "method": enum_codec(ScoreMethod),
     "created_at": TIME},
    order_by="created_at, id",
    generated=("id",),
)


def append(conn: sqlite3.Connection, record: ScoreRecord) -> int:
    if record.id is not None:
        raise ValueError("追加的评分不能自带编号")
    return TABLE.insert(conn, record)


def find(
    conn: sqlite3.Connection,
    *,
    stage: Stage | None = None,
    run_id: str | None = None,
    subject_id: str | None = None,
    item: str | None = None,
) -> list[ScoreRecord]:
    return TABLE.find(conn, **given({"stage": stage, "run_id": run_id, "subject_id": subject_id, "item": item}))
