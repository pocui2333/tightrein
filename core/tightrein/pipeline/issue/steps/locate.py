"""按根因位置查找未关闭的 Issue(architecture/06 10.1 第 2 步)：幂等键为「文件 + 所在方法」的集合。

Issue 文件的 rootCause 只有「文件:行号」，方法名取自该 Issue 各问题最近一次分诊的根因；没有方法名时用行号。
与某个未关闭 Issue 的键有交集即视为同一根因。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from tightrein.store.repos import issues, triage
from tightrein.store.repos.issues import IssueRecord


def key(root_causes: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    return frozenset(f"{cause['file']}#{cause.get('symbol') or cause['line']}" for cause in root_causes)


def _issue_key(conn: sqlite3.Connection, record: IssueRecord) -> frozenset[str]:
    found: set[str] = set()
    for problem_id in record.issue.problems:
        latest = triage.latest(conn, problem_id)
        if latest is not None:
            found |= key({"file": cause.file, "line": cause.line, "symbol": cause.symbol}
                         for cause in latest.result.root_causes)
    return frozenset(found)


def find(conn: sqlite3.Connection, root_causes: Iterable[Mapping[str, Any]]) -> IssueRecord | None:
    wanted = key(root_causes)
    if not wanted:
        return None
    for record in issues.find(conn):
        if not record.issue.is_closed and wanted & _issue_key(conn, record):
            return record
    return None
