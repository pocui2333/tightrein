"""runs 表：运行索引(编号、阶段、触发方式、状态、心跳与持有者)。交接内容与量化数据在运行目录的文件里。

开始运行时记下持有它的进程号与主机，启动恢复据此与心跳判断它是否还在执行(recovery.md)。
"""

from __future__ import annotations

import os
import socket
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from tightrein.protocol.naming import Clock, format_iso, run_id
from tightrein.store.tables.table import Table

TRIGGERS = ("schedule", "event", "manual")
STATUSES = ("running", "done", "failed", "interrupted", "skipped")
RUNNING = "running"


@dataclass
class Run:
    id: str
    stage: str
    trigger: str | None
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    heartbeat_at: datetime | None = None
    holder_pid: int | None = None
    holder_host: str | None = None
    summary: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"运行状态只能是 {', '.join(STATUSES)}：{self.status}")
        if self.trigger is not None and self.trigger not in TRIGGERS:
            raise ValueError(f"触发方式只能是 {', '.join(TRIGGERS)}：{self.trigger}")


TABLE = Table(
    "runs", Run,
    times=("started_at", "ended_at", "heartbeat_at"), json_columns=("summary",), order_by='"started_at", "id"',
)


def start(conn: sqlite3.Connection, run: Run) -> None:
    """登记一次新运行。没给持有者时取当前进程；心跳从开始时间算起。"""
    if run.holder_pid is None:
        run = replace(run, holder_pid=os.getpid(), holder_host=socket.gethostname())
    if run.heartbeat_at is None:
        run = replace(run, heartbeat_at=run.started_at)
    TABLE.insert(conn, run, run.started_at)


def heartbeat(conn: sqlite3.Connection, run_id: str, clock: Clock) -> None:
    now = format_iso(clock.now())
    conn.execute("UPDATE runs SET heartbeat_at = ?, updated_at = ? WHERE id = ?", (now, now, run_id))


def finish(
    conn: sqlite3.Connection, run_id: str, status: str, clock: Clock, summary: dict[str, Any] | None = None
) -> None:
    """结束运行；summary 为空时保留原有的摘要。"""
    if status not in STATUSES or status == RUNNING:
        raise ValueError(f"结束状态不合格：{status}")
    now = format_iso(clock.now())
    conn.execute(
        "UPDATE runs SET status = ?, ended_at = ?, summary = COALESCE(?, summary), updated_at = ? WHERE id = ?",
        (status, now, TABLE.encode("summary", summary), now, run_id),
    )


def get(conn: sqlite3.Connection, run_id: str) -> Run | None:
    return TABLE.get(conn, run_id)


def running(conn: sqlite3.Connection) -> list[Run]:
    return TABLE.find(conn, status=RUNNING)


def latest(conn: sqlite3.Connection, stage: str | None = None) -> Run | None:
    if stage is None:
        row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM runs WHERE stage = ? ORDER BY started_at DESC, id DESC LIMIT 1", (stage,)
        ).fetchone()
    return None if row is None else TABLE.from_row(row)


def free_id(conn: sqlite3.Connection, now: datetime, stage: str) -> str:
    """新运行的编号。编号精确到秒：同一阶段同一秒内已有运行时顺延一秒，不覆盖已有的记录与目录。"""
    candidate = now
    while True:
        identifier = run_id(candidate, stage)
        if conn.execute("SELECT 1 FROM runs WHERE id = ?", (identifier,)).fetchone() is None:
            return identifier
        candidate += timedelta(seconds=1)
