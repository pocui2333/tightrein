"""operations 表：对外写操作的幂等键(recovery.md「写操作幂等」)。

执行前写「进行中」，成功后改「已完成」并存结果，再次执行直接返回上次结果。
动作抛出普通异常说明没有做成，删键允许重试；中断(Interrupted、KeyboardInterrupt)或进程被杀时键停在「进行中」，
再次执行抛 InProgress：远端可能已经做了，要先到远端核对，再调 complete 或 abandon。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tightrein.protocol.naming import Clock, format_iso
from tightrein.store.db import transaction
from tightrein.store.tables.table import Table

IN_PROGRESS = "in_progress"
DONE = "done"


@dataclass
class Operation:
    key: str
    status: str
    result: str | None = None  # 按 run_once 的 encode 编码的文本
    subject: str | None = None
    point: str | None = None

    def __post_init__(self) -> None:
        if self.status not in (IN_PROGRESS, DONE):
            raise ValueError(f"幂等键的状态只能是 {IN_PROGRESS} 或 {DONE}：{self.status}")


class InProgress(Exception):
    """幂等键处于进行中：上一次执行可能被中断，需要先到远端核对实际状态。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"幂等键 {key} 处于进行中，先核对实际状态再标完成或放弃")
        self.key = key


TABLE = Table("operations", Operation, key="key", order_by='"created_at", "key"')


def get(conn: sqlite3.Connection, key: str) -> Operation | None:
    return TABLE.get(conn, key)


def find(conn: sqlite3.Connection, *, subject: str | None = None, status: str | None = None) -> list[Operation]:
    return TABLE.find(conn, subject=subject, status=status)


def run_once[T](
    conn: sqlite3.Connection,
    key: str,
    action: Callable[[], T],
    clock: Clock,
    *,
    encode: Callable[[Any], str] = json.dumps,
    decode: Callable[[str], Any] = json.loads,
    subject: str | None = None,
    point: str | None = None,
) -> T:
    """同一个键只执行一次 action：已完成时不执行，返回上次的结果；处于进行中时抛 InProgress。"""
    with transaction(conn):
        existing = get(conn, key)
        if existing is None:
            TABLE.save(conn, Operation(key, IN_PROGRESS, subject=subject, point=point), clock.now())
    if existing is not None:
        if existing.status == DONE:
            return decode(existing.result) if existing.result is not None else None  # type: ignore[return-value]
        raise InProgress(key)
    try:
        result = action()
    except Exception:
        abandon(conn, key)
        raise
    complete(conn, key, result, clock, encode=encode)
    return result


def complete(
    conn: sqlite3.Connection, key: str, result: Any, clock: Clock, *, encode: Callable[[Any], str] = json.dumps
) -> None:
    """标为已完成并存结果；也用于对账后补记(键不存在时新建)。"""
    now = format_iso(clock.now())
    conn.execute(
        'INSERT INTO operations ("key", status, result, created_at, updated_at) VALUES (?, ?, ?, ?, ?) '
        "ON CONFLICT (\"key\") DO UPDATE SET status = excluded.status, result = excluded.result, "
        "updated_at = excluded.updated_at",
        (key, DONE, encode(result), now, now),
    )


def abandon(conn: sqlite3.Connection, key: str) -> None:
    """删除进行中的键，允许重新执行；已完成的键不删。"""
    conn.execute('DELETE FROM operations WHERE "key" = ? AND status = ?', (key, IN_PROGRESS))
