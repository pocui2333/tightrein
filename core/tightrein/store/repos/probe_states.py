"""probe_states 表：项目探针上次运行的时间与它留下的状态(redesign/01-collect.md 第 4 节)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tightrein.store.repos.table import JSON, TIME, Table


@dataclass(frozen=True)
class ProbeState:
    name: str
    last_run_at: datetime
    state: dict[str, Any] | None = None


TABLE = Table("probe_states", ProbeState, ("name",), {"last_run_at": TIME, "state": JSON}, order_by="name")


def save(conn: sqlite3.Connection, state: ProbeState) -> None:
    TABLE.save(conn, state)


def get(conn: sqlite3.Connection, name: str) -> ProbeState | None:
    return TABLE.get(conn, name=name)
