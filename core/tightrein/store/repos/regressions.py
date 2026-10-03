"""regressions 表：复现检查清单与最近结果，键为(Issue、检查编号)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime

from tightrein.domain.enums import RegressionKind, RegressionResult
from tightrein.store.repos.table import STRINGS, TIME, Table, enum_codec, given


@dataclass(frozen=True)
class RegressionCheck:
    issue_id: str
    check_id: str
    kind: RegressionKind
    path: str
    hash: str
    last_result: RegressionResult = RegressionResult.NOT_RUN
    requires: tuple[str, ...] = ()
    base_commit: str | None = None
    base_result: RegressionResult | None = None
    last_run_id: str | None = None
    last_run_at: datetime | None = None
    last_release: str | None = None


TABLE = Table(
    "regressions",
    RegressionCheck,
    ("issue_id", "check_id"),
    {"kind": enum_codec(RegressionKind), "last_result": enum_codec(RegressionResult), "requires": STRINGS,
     "base_result": enum_codec(RegressionResult), "last_run_at": TIME},
    order_by="issue_id, check_id",
)


def save(conn: sqlite3.Connection, check: RegressionCheck) -> None:
    TABLE.save(conn, check)


def get(conn: sqlite3.Connection, issue_id: str, check_id: str) -> RegressionCheck | None:
    return TABLE.get(conn, issue_id=issue_id, check_id=check_id)


def find(
    conn: sqlite3.Connection, *, issue_id: str | None = None, last_result: RegressionResult | None = None
) -> list[RegressionCheck]:
    return TABLE.find(conn, **given({"issue_id": issue_id, "last_result": last_result}))


def record_result(
    conn: sqlite3.Connection,
    issue_id: str,
    check_id: str,
    result: RegressionResult,
    run_id: str,
    at: datetime,
    release: str | None,
) -> RegressionCheck:
    """记录一次执行的结果，返回更新后的检查。"""
    check = get(conn, issue_id, check_id)
    if check is None:
        raise LookupError(f"Issue {issue_id} 没有复现检查 {check_id}")
    updated = replace(check, last_result=result, last_run_id=run_id, last_run_at=at, last_release=release)
    save(conn, updated)
    return updated
