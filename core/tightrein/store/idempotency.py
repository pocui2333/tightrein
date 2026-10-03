"""对外操作的幂等键(architecture/01 4.4，design 15.7)。

执行前写入 in-progress，成功后改为 done 并记录结果；再次执行时已完成的直接跳过并返回上次的结果。
操作抛出异常说明没有完成，删除键以便重试；进程在执行中被中断时键停在 in-progress，由调用方核对实际状态后
调用 complete 或 abandon(architecture/02 4.4)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tightrein.domain.clock import Clock, format_iso
from tightrein.store.db import transaction
from tightrein.store.repos.table import JSON, TIME, Table

IN_PROGRESS = "in-progress"
DONE = "done"

Result = dict[str, Any]


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    status: str
    created_at: datetime
    result: Result | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.status not in (IN_PROGRESS, DONE):
            raise ValueError(f"幂等键的状态只能是 {IN_PROGRESS} 或 {DONE}：{self.status}")


@dataclass(frozen=True)
class Begun:
    record: IdempotencyRecord
    created: bool


@dataclass(frozen=True)
class Outcome:
    result: Result
    skipped: bool


class InProgress(Exception):
    """幂等键处于进行中：上一次执行可能被中断，需要核对实际状态。"""

    def __init__(self, record: IdempotencyRecord) -> None:
        self.record = record
        super().__init__(f"幂等键 {record.key} 自 {format_iso(record.created_at)} 起处于进行中，先核对实际状态")


TABLE = Table(
    "idempotency_keys", IdempotencyRecord, ("key",),
    {"created_at": TIME, "result": JSON, "completed_at": TIME}, order_by="created_at, key",
)


def get(conn: sqlite3.Connection, key: str) -> IdempotencyRecord | None:
    return TABLE.get(conn, key=key)


def find(conn: sqlite3.Connection, prefix: str) -> list[IdempotencyRecord]:
    """键以 prefix 开头的全部记录，按创建时间排序。"""
    return [record for record in TABLE.find(conn) if record.key.startswith(prefix)]


def begin(conn: sqlite3.Connection, key: str, clock: Clock) -> Begun:
    """写入进行中的键；键已存在时不修改，返回已有记录且 created 为假。"""
    with transaction(conn):
        existing = get(conn, key)
        if existing is not None:
            return Begun(existing, created=False)
        record = IdempotencyRecord(key, IN_PROGRESS, clock.now())
        TABLE.save(conn, record)
    return Begun(record, created=True)


def complete(conn: sqlite3.Connection, key: str, result: Result, clock: Clock) -> IdempotencyRecord:
    with transaction(conn):
        existing = get(conn, key)
        if existing is None:
            raise LookupError(f"幂等键 {key} 不存在")
        record = IdempotencyRecord(key, DONE, existing.created_at, result, clock.now())
        TABLE.save(conn, record)
    return record


def abandon(conn: sqlite3.Connection, key: str) -> bool:
    """删除进行中的键，允许重新执行；已完成的键不删除，返回 False。"""
    cursor = conn.execute("DELETE FROM idempotency_keys WHERE key = ? AND status = ?", (key, IN_PROGRESS))
    return cursor.rowcount > 0


def run_once(conn: sqlite3.Connection, key: str, action: Callable[[], Result], clock: Clock) -> Outcome:
    """同一个键只执行一次 action：已完成时跳过并返回上次的结果；处于进行中时抛出 InProgress。"""
    begun = begin(conn, key, clock)
    if not begun.created:
        if begun.record.status == DONE:
            return Outcome(begun.record.result or {}, skipped=True)
        raise InProgress(begun.record)
    try:
        result = action()
    except BaseException:
        abandon(conn, key)
        raise
    complete(conn, key, result, clock)
    return Outcome(result, skipped=False)
