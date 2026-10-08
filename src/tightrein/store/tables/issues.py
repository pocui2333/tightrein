"""issues 表：每个 Issue 一行(状态、所在步骤、分支、PR、合并提交、部署)。

状态取值与转换由 assess/issue 的状态机管(44c)；事件写进记录日志，这里只存当前值。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from tightrein.protocol.naming import Clock
from tightrein.store.tables.table import Table


@dataclass
class Issue:
    id: str
    status: str
    title: str
    kind: str  # bug、feature
    origin: str  # problem、user
    severity: str | None = None
    gate: str | None = None
    stage: str | None = None
    step: str | None = None
    round: int | None = None
    branch: str | None = None
    pr: int | None = None
    merge_commit: str | None = None
    deploy: str | None = None
    held_by: str | None = None  # 手动接管者；有值时 tightrein 不再碰它
    extra: dict[str, Any] = field(default_factory=dict)


TABLE = Table("issues", Issue, json_columns=("extra",), order_by="id")


def get(conn: sqlite3.Connection, issue_id: str) -> Issue | None:
    return TABLE.get(conn, issue_id)


def find(conn: sqlite3.Connection, *, status: str | None = None) -> list[Issue]:
    return TABLE.find(conn, status=status)


def save(conn: sqlite3.Connection, issue: Issue, clock: Clock) -> None:
    TABLE.save(conn, issue, clock.now())
