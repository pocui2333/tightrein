"""pending_operations 表：待确认操作(architecture/01 4.2、architecture/02 4.4)。

commands、description、preconditions、result 以 JSON 形式存取；vcs 中的 PendingOperation与本记录互转，
状态流转的规则也在 vcs，本模块只读写。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.domain.enums import OperationExecutor, OperationKind, OperationStatus, Stage
from tightrein.store.repos.table import BOOL, JSON, TIME, Table, enum_codec, given

MAX_CONFIRMATIONS = 2


@dataclass(frozen=True)
class PendingOperationRecord:
    id: str
    stage: Stage
    subject_id: str
    kind: OperationKind
    executor: OperationExecutor
    impact: str
    reversible: bool
    idempotency_key: str
    confirmations_required: int
    status: OperationStatus
    created_at: datetime
    commands: list[dict[str, Any]] = field(default_factory=list)
    description: dict[str, Any] = field(default_factory=dict)
    preconditions: dict[str, Any] = field(default_factory=dict)
    confirmations_given: int = 0
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.confirmations_required <= MAX_CONFIRMATIONS:
            raise ValueError(f"确认次数只能是 1 或 {MAX_CONFIRMATIONS}：{self.confirmations_required}")
        if not 0 <= self.confirmations_given <= self.confirmations_required:
            raise ValueError(f"已确认次数超出范围：{self.confirmations_given}")


TABLE = Table(
    "pending_operations",
    PendingOperationRecord,
    ("id",),
    {
        "stage": enum_codec(Stage),
        "kind": enum_codec(OperationKind),
        "executor": enum_codec(OperationExecutor),
        "reversible": BOOL,
        "status": enum_codec(OperationStatus),
        "created_at": TIME,
        "commands": JSON,
        "description": JSON,
        "preconditions": JSON,
        "decided_at": TIME,
        "executed_at": TIME,
        "result": JSON,
    },
    order_by="created_at, id",
)


def save(conn: sqlite3.Connection, record: PendingOperationRecord) -> None:
    TABLE.save(conn, record)


def get(conn: sqlite3.Connection, operation_id: str) -> PendingOperationRecord | None:
    return TABLE.get(conn, id=operation_id)


def find(
    conn: sqlite3.Connection, *, subject_id: str | None = None, status: OperationStatus | None = None
) -> list[PendingOperationRecord]:
    return TABLE.find(conn, **given({"subject_id": subject_id, "status": status}))
