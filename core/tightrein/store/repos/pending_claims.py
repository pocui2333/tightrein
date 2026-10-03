"""pending_claims 表：静态巡检中尚未取证的疑点(redesign/01-collect.md 第 6 节)。

reason 为 low(低级疑点，选中时再取证)或 over-limit(超出本次取证上限，后续运行继续处理)；state 为 pending、
verified(已取证，结论随信号产出)。疑点按文件、行与规则去重，同一疑点再次被审查出来时保留原记录。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tightrein.store.repos.table import JSON, TIME, Table

LOW = "low"
OVER_LIMIT = "over-limit"
PENDING = "pending"
VERIFIED = "verified"


@dataclass(frozen=True)
class PendingClaim:
    id: str
    claim: dict[str, Any]
    severity: str | None
    reason: str
    run_id: str
    created_at: datetime
    state: str = PENDING


TABLE = Table("pending_claims", PendingClaim, ("id",), {"claim": JSON, "created_at": TIME}, order_by="created_at, id")


def save(conn: sqlite3.Connection, claim: PendingClaim) -> None:
    TABLE.save(conn, claim)


def get(conn: sqlite3.Connection, claim_id: str) -> PendingClaim | None:
    return TABLE.get(conn, id=claim_id)


def find(conn: sqlite3.Connection, *, reason: str | None = None, state: str = PENDING) -> list[PendingClaim]:
    filters: dict[str, Any] = {"state": state}
    if reason is not None:
        filters["reason"] = reason
    return TABLE.find(conn, **filters)
