"""编号分配(architecture/01 1.1)：sequences 表中每个序列一行，递增在一条语句内完成，并发写入时不重复。

在外层事务中分配的编号随事务回滚一并撤销，不会留下空号以外的副作用。
"""

from __future__ import annotations

import sqlite3

from tightrein.domain import ids
from tightrein.domain.enums import KnowledgeType

PROBLEM = "problem"
ISSUE = "issue"
OPERATION = "operation"
SUGGESTION = "suggestion"


def next_value(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "INSERT INTO sequences (name, value) VALUES (?, 1) "
        "ON CONFLICT (name) DO UPDATE SET value = value + 1 RETURNING value",
        (name,),
    ).fetchone()
    return int(row[0])


def current(conn: sqlite3.Connection, name: str) -> int:
    """最近分配的值；还没有分配过时为 0。"""
    row = conn.execute("SELECT value FROM sequences WHERE name = ?", (name,)).fetchone()
    return 0 if row is None else int(row[0])


def ensure_at_least(conn: sqlite3.Connection, name: str, value: int) -> None:
    """保证下一次分配大于 value；用于从文件重建索引后，避免再分配已被文件占用的编号。"""
    conn.execute(
        "INSERT INTO sequences (name, value) VALUES (?, ?) "
        "ON CONFLICT (name) DO UPDATE SET value = MAX(value, excluded.value)",
        (name, value),
    )


def next_issue_id(conn: sqlite3.Connection) -> str:
    return ids.issue_id(next_value(conn, ISSUE))


def next_operation_id(conn: sqlite3.Connection) -> str:
    return ids.operation_id(next_value(conn, OPERATION))


def next_suggestion_id(conn: sqlite3.Connection) -> str:
    return ids.suggestion_id(next_value(conn, SUGGESTION))


def next_knowledge_id(conn: sqlite3.Connection, kind: KnowledgeType) -> str:
    return ids.knowledge_id(kind, next_value(conn, ids.knowledge_sequence(kind)))
