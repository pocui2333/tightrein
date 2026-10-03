"""signals 表：信号。context 与 actor 为 JSON 对象。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import Probe, SignalAggregateState, Source
from tightrein.domain.signal import Signal
from tightrein.store.db import transaction
from tightrein.store.repos.table import BOOL, JSON, TIME, delete, enum_codec, given, select, upsert

TABLE = "signals"
_ORDER = "occurred_at, id"


def to_row(signal: Signal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "run_id": signal.run_id,
        "source": signal.source.value,
        "probe": signal.probe.value,
        "check": signal.check,
        "environment": signal.environment,
        "occurred_at": format_iso(signal.occurred_at),
        "release": signal.release,
        "location": signal.location,
        "message": signal.message,
        "normalized_message": signal.normalized_message,
        "context": JSON.encode(signal.context),
        "actor": JSON.encode(signal.actor),
        "fingerprint": signal.fingerprint,
        "suppressed": BOOL.encode(signal.suppressed),
        "aggregate_state": signal.aggregate_state.value,
    }


def from_row(row: sqlite3.Row) -> Signal:
    return Signal(
        id=row["id"],
        run_id=row["run_id"],
        source=Source(row["source"]),
        probe=Probe(row["probe"]),
        check=row["check"],
        environment=row["environment"],
        occurred_at=TIME.decode(row["occurred_at"]),
        release=row["release"],
        location=row["location"],
        message=row["message"],
        context=JSON.decode(row["context"]),
        actor=JSON.decode(row["actor"]),
        normalized_message=row["normalized_message"],
        fingerprint=row["fingerprint"],
        suppressed=BOOL.decode(row["suppressed"]),
        aggregate_state=SignalAggregateState(row["aggregate_state"]),
    )


def save(conn: sqlite3.Connection, signal: Signal) -> None:
    upsert(conn, TABLE, to_row(signal), ("id",))


def save_all(conn: sqlite3.Connection, signals: Iterable[Signal]) -> None:
    """在一个事务中写入全部信号，任何一条失败时整体不写入。"""
    with transaction(conn):
        for signal in signals:
            save(conn, signal)


def get(conn: sqlite3.Connection, signal_id: str) -> Signal | None:
    rows = select(conn, TABLE, {"id": signal_id})
    return from_row(rows[0]) if rows else None


def find(
    conn: sqlite3.Connection,
    *,
    run_id: str | None = None,
    fingerprint: str | None = None,
    probe: Probe | None = None,
    aggregate_state: SignalAggregateState | None = None,
) -> list[Signal]:
    """按给出的条件等值过滤，按发生时间升序。"""
    filters = {
        "run_id": run_id,
        "fingerprint": fingerprint,
        "probe": enum_codec(Probe).to_column(probe),
        "aggregate_state": enum_codec(SignalAggregateState).to_column(aggregate_state),
    }
    return [from_row(row) for row in select(conn, TABLE, given(filters), _ORDER)]


def get_many(conn: sqlite3.Connection, signal_ids: Iterable[str]) -> list[Signal]:
    """按编号取信号，按发生时间升序；不存在的编号被忽略。"""
    wanted = list(signal_ids)
    if not wanted:
        return []
    marks = ", ".join("?" for _ in wanted)
    rows = conn.execute(f"SELECT * FROM signals WHERE id IN ({marks}) ORDER BY {_ORDER}", wanted).fetchall()
    return [from_row(row) for row in rows]


def delete_for_run(conn: sqlite3.Connection, run_id: str) -> int:
    """删除一个运行的全部信号，返回删除的条数；用于重新解析尚未聚合的运行。"""
    return delete(conn, TABLE, {"run_id": run_id})


def reset_aggregation(conn: sqlite3.Connection) -> None:
    """整体重放前清除全部信号的聚合字段。"""
    conn.execute(
        "UPDATE signals SET normalized_message = NULL, fingerprint = NULL, suppressed = 0, aggregate_state = ?",
        (SignalAggregateState.PENDING.value,),
    )
