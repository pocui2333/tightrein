"""problems、problem_signals、problem_aliases 三张表：问题、问题与信号的对应、合并产生的别名。

scope 与 ignore_until 写入前按 common.schema.json 中的定义校验。merge 由聚合的 merge 命令与分诊的查重共用
(architecture/01 4.1)，合并的数据变化只有这一份；状态事件由调用方写入 problem_events。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from tightrein.contracts import validate as contracts
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import Probe, ProblemStatus
from tightrein.domain.ids import parse_sequence
from tightrein.domain.problem import IgnoreCondition, Problem, ProblemScope
from tightrein.store.db import transaction
from tightrein.store.repos.table import BOOL, JSON, TIME, enum_codec, given, select, upsert, where

TABLE = "problems"


class MergeRejected(Exception):
    """合并的前提不满足：两个编号相同、问题不存在、被并入的问题已有 Issue 或已被合并、目标已被合并。"""


def to_row(problem: Problem) -> dict[str, Any]:
    scope = problem.scope.to_dict()
    contracts.check("common.schema.json", scope, definition="problemScope")
    ignore_until = problem.ignore_until.to_dict() if problem.ignore_until is not None else None
    if ignore_until is not None:
        contracts.check("common.schema.json", ignore_until, definition="ignoreCondition")
    return {
        "id": problem.id,
        "fingerprint": problem.fingerprint,
        "fingerprint_version": problem.fingerprint_version,
        "probe": problem.probe.value,
        "title": problem.title,
        "status": problem.status.value,
        "first_seen_at": format_iso(problem.first_seen_at),
        "last_seen_at": format_iso(problem.last_seen_at),
        "first_seen_release": problem.first_seen_release,
        "last_seen_release": problem.last_seen_release,
        "resolved_release": problem.resolved_release,
        "occurrences": problem.occurrences,
        "issue_id": problem.issue_id,
        "ignore_until": JSON.to_column(ignore_until),
        "intermittent": BOOL.encode(problem.intermittent),
        "clean_covered_runs": problem.clean_covered_runs,
        "merged_into": problem.merged_into,
        "scope": JSON.encode(scope),
    }


def from_row(row: sqlite3.Row) -> Problem:
    ignore_until = JSON.from_column(row["ignore_until"])
    return Problem(
        id=row["id"],
        fingerprint=row["fingerprint"],
        fingerprint_version=row["fingerprint_version"],
        probe=Probe(row["probe"]),
        title=row["title"],
        status=ProblemStatus(row["status"]),
        first_seen_at=TIME.decode(row["first_seen_at"]),
        last_seen_at=TIME.decode(row["last_seen_at"]),
        scope=ProblemScope.from_dict(JSON.decode(row["scope"])),
        first_seen_release=row["first_seen_release"],
        last_seen_release=row["last_seen_release"],
        resolved_release=row["resolved_release"],
        occurrences=row["occurrences"],
        issue_id=row["issue_id"],
        ignore_until=IgnoreCondition.from_dict(ignore_until) if ignore_until is not None else None,
        intermittent=BOOL.decode(row["intermittent"]),
        clean_covered_runs=row["clean_covered_runs"],
        merged_into=row["merged_into"],
    )


def save(conn: sqlite3.Connection, problem: Problem) -> None:
    upsert(conn, TABLE, to_row(problem), ("id",))


def get(conn: sqlite3.Connection, problem_id: str) -> Problem | None:
    rows = select(conn, TABLE, {"id": problem_id})
    return from_row(rows[0]) if rows else None


def find(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[ProblemStatus] | None = None,
    probe: Probe | None = None,
    issue_id: str | None = None,
) -> list[Problem]:
    """按给出的条件过滤，statuses 为取值之一即可；按编号升序。"""
    clause, params = where(given({"probe": enum_codec(Probe).to_column(probe), "issue_id": issue_id}))
    if statuses is not None:
        wanted = [status.value for status in statuses]
        if not wanted:
            return []
        marks = ", ".join("?" for _ in wanted)
        clause += (" AND " if clause else " WHERE ") + f"status IN ({marks})"
        params += wanted
    rows = conn.execute(f"SELECT * FROM problems{clause}", params).fetchall()
    return sorted((from_row(row) for row in rows), key=lambda problem: parse_sequence(problem.id))


def by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> Problem | None:
    """指纹归属的问题：先按问题自身的指纹、再按合并产生的别名查找；找到的问题已被合并时返回合并目标。"""
    rows = select(conn, TABLE, {"fingerprint": fingerprint})
    if rows:
        problem: Problem | None = from_row(rows[0])
    else:
        alias = conn.execute("SELECT problem_id FROM problem_aliases WHERE fingerprint = ?", (fingerprint,)).fetchone()
        problem = get(conn, alias["problem_id"]) if alias is not None else None
    while problem is not None and problem.merged_into is not None:
        problem = get(conn, problem.merged_into)
    return problem


def add_signals(conn: sqlite3.Connection, problem_id: str, signal_ids: Iterable[str]) -> None:
    """记录问题与信号的对应；已记录的对应被忽略。"""
    conn.executemany(
        "INSERT OR IGNORE INTO problem_signals (problem_id, signal_id) VALUES (?, ?)",
        [(problem_id, signal_id) for signal_id in signal_ids],
    )


def signal_ids(conn: sqlite3.Connection, problem_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT signal_id FROM problem_signals WHERE problem_id = ? ORDER BY signal_id", (problem_id,)
    ).fetchall()
    return [row["signal_id"] for row in rows]


def add_alias(conn: sqlite3.Connection, fingerprint: str, problem_id: str, clock: Clock) -> None:
    upsert(
        conn,
        "problem_aliases",
        {"fingerprint": fingerprint, "problem_id": problem_id, "created_at": format_iso(clock.now())},
        ("fingerprint",),
    )


def aliases(conn: sqlite3.Connection, problem_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT fingerprint FROM problem_aliases WHERE problem_id = ? ORDER BY fingerprint", (problem_id,)
    ).fetchall()
    return [row["fingerprint"] for row in rows]


def merge(conn: sqlite3.Connection, target_id: str, source_id: str, clock: Clock) -> Problem:
    """把 source 并入 target(design 2.9)，返回并入后的 source。

    source 的信号对应改到 target；source 的指纹与已有别名都指向 target；source 的 merged_into 记为 target。
    """
    if target_id == source_id:
        raise MergeRejected(f"不能把问题 {source_id} 并入自身")
    target, source = get(conn, target_id), get(conn, source_id)
    if target is None or source is None:
        raise MergeRejected(f"问题不存在：{target_id if target is None else source_id}")
    if source.issue_id is not None:
        raise MergeRejected(f"问题 {source_id} 已有 Issue {source.issue_id}，先按 Issue 与问题的同步规则处理 Issue")
    if source.merged_into is not None:
        raise MergeRejected(f"问题 {source_id} 已并入 {source.merged_into}")
    if target.merged_into is not None:
        raise MergeRejected(f"问题 {target_id} 已并入 {target.merged_into}，应并入 {target.merged_into}")
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO problem_signals (problem_id, signal_id) "
            "SELECT ?, signal_id FROM problem_signals WHERE problem_id = ?",
            (target_id, source_id),
        )
        conn.execute("DELETE FROM problem_signals WHERE problem_id = ?", (source_id,))
        conn.execute("UPDATE problem_aliases SET problem_id = ? WHERE problem_id = ?", (target_id, source_id))
        add_alias(conn, source.fingerprint, target_id, clock)
        merged = replace(source, merged_into=target_id)
        save(conn, merged)
    return merged


def clear(conn: sqlite3.Connection) -> None:
    """整体重放前删除全部问题；problem_signals、problem_aliases、problem_events 随外键一并删除。"""
    conn.execute("DELETE FROM problems")
