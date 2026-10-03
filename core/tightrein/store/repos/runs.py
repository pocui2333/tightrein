"""runs 表：运行记录与覆盖范围。coverage 与 environment_detail 写入前按 common.schema.json 中的定义校验。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from tightrein.contracts import validate as contracts
from tightrein.domain import ids
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import Probe, ProbeLevel, RunStage, RunStatus
from tightrein.domain.run import Coverage, EnvironmentDetail, Run
from tightrein.store.repos.table import JSON, TIME, enum_codec, given, select, upsert

TABLE = "runs"
_ORDER = "started_at, id"


def to_row(run: Run) -> dict[str, Any]:
    coverage = run.coverage.to_dict()
    detail = run.environment_detail.to_dict()
    contracts.check("common.schema.json", coverage, definition="coverage")
    contracts.check("common.schema.json", detail, definition="environmentDetail")
    return {
        "id": run.id,
        "stage": run.stage.value,
        "probe": enum_codec(Probe).to_column(run.probe),
        "level": enum_codec(ProbeLevel).to_column(run.level),
        "parent_run_id": run.parent_run_id,
        "started_at": format_iso(run.started_at),
        "ended_at": TIME.to_column(run.ended_at),
        "target_commit": run.target_commit,
        "coverage": JSON.encode(coverage),
        "environment_detail": JSON.encode(detail),
        "status": run.status.value,
        "aggregated_at": TIME.to_column(run.aggregated_at),
        "trace_id": run.trace_id,
    }


def from_row(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        stage=RunStage(row["stage"]),
        started_at=TIME.decode(row["started_at"]),
        status=RunStatus(row["status"]),
        probe=enum_codec(Probe).from_column(row["probe"]),
        level=enum_codec(ProbeLevel).from_column(row["level"]),
        parent_run_id=row["parent_run_id"],
        ended_at=TIME.from_column(row["ended_at"]),
        target_commit=row["target_commit"],
        coverage=Coverage.from_dict(JSON.decode(row["coverage"])),
        environment_detail=EnvironmentDetail.from_dict(JSON.decode(row["environment_detail"])),
        aggregated_at=TIME.from_column(row["aggregated_at"]),
        trace_id=row["trace_id"],
    )


def save(conn: sqlite3.Connection, run: Run) -> None:
    upsert(conn, TABLE, to_row(run), ("id",))


def free_id(conn: sqlite3.Connection, started: datetime, stage: RunStage, probe: Probe | None = None) -> str:
    """新运行的编号。编号精确到秒，同一环节同一秒内已有运行时依次取下一秒的编号，不覆盖已有的运行记录与目录。"""
    moment = started
    while True:
        run_id = ids.run_id(moment, stage, probe)
        if get(conn, run_id) is None:
            return run_id
        moment += timedelta(seconds=1)


def get(conn: sqlite3.Connection, run_id: str) -> Run | None:
    rows = select(conn, TABLE, {"id": run_id})
    return from_row(rows[0]) if rows else None


def find(
    conn: sqlite3.Connection,
    *,
    stage: RunStage | None = None,
    probe: Probe | None = None,
    status: RunStatus | None = None,
    parent_run_id: str | None = None,
) -> list[Run]:
    """按给出的条件等值过滤，未给出的条件不参与过滤；按开始时间升序。"""
    filters = {
        "stage": enum_codec(RunStage).to_column(stage),
        "probe": enum_codec(Probe).to_column(probe),
        "status": enum_codec(RunStatus).to_column(status),
        "parent_run_id": parent_run_id,
    }
    return [from_row(row) for row in select(conn, TABLE, given(filters), _ORDER)]


def unaggregated(conn: sqlite3.Connection) -> list[Run]:
    """已结束、尚未被聚合的 collect 运行，按开始时间升序。"""
    rows = conn.execute(
        f"SELECT * FROM runs WHERE stage = ? AND aggregated_at IS NULL AND status != ? ORDER BY {_ORDER}",
        (RunStage.COLLECT.value, RunStatus.RUNNING.value),
    ).fetchall()
    return [from_row(row) for row in rows]


def mark_aggregated(conn: sqlite3.Connection, run_ids: Iterable[str], at: datetime) -> None:
    conn.executemany(
        "UPDATE runs SET aggregated_at = ? WHERE id = ?", [(format_iso(at), run_id) for run_id in run_ids]
    )


def reset_aggregation(conn: sqlite3.Connection) -> None:
    """整体重放前把全部 collect 运行复位为未聚合。"""
    conn.execute("UPDATE runs SET aggregated_at = NULL WHERE stage = ?", (RunStage.COLLECT.value,))
