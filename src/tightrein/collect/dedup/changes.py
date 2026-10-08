"""变更集：一次去重中各步只改内存里的它，最后由 dedup.py 在一个事务中写入。

查找问题时先看本次内存中已新建或改过的，再看从数据库批量读入的；同一指纹的第二条信号不会再建一个问题，
合并产生的别名指纹也能命中。新问题的编号按序列当前值在内存中顺延，写入时推进序列。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.collect.common.signals import Signal
from tightrein.collect.dedup import status as status_rules
from tightrein.protocol.naming import format_iso, parse_iso, problem_id
from tightrein.store.tables import problems, sequences
from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem

# problems.extra 中的键(写进 JSON 为小驼峰)
ALIASES = "aliases"  # 跨来源合并、并入的其他指纹
SOURCES = "sources"  # 报过这个问题的全部来源
MERGED_INTO = "mergedInto"


@dataclass(frozen=True)
class Transition:
    problem: str
    event: str
    before: str
    after: str
    context: dict[str, Any]
    at: str


@dataclass
class Reproduction:
    strategy: str  # replay、immediate
    attempts: int = 0
    result: str | None = None  # reproduced、not_reproduced、unavailable；没有重放为 None


@dataclass
class ChangeSet:
    run: str
    now: datetime
    next_number: int
    known: dict[str, Problem] = field(default_factory=dict)
    index: dict[str, str] = field(default_factory=dict)  # 指纹(含别名) → 问题编号
    touched: set[str] = field(default_factory=set)
    created: list[str] = field(default_factory=list)
    occurred: dict[str, list[Signal]] = field(default_factory=dict)
    occurrences: list[Occurrence] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    reopened: list[tuple[str, str]] = field(default_factory=list)  # (Issue, 问题)
    reproduction: dict[str, Reproduction] = field(default_factory=dict)
    regression_checks: set[str] = field(default_factory=set)  # 本次复现检查(回归信号)失败的问题
    states: dict[str, Any] = field(default_factory=dict)  # 与问题同事务写进 state 表
    notes: list[str] = field(default_factory=list)
    suppressed: int = 0
    merged: int = 0

    @classmethod
    def start(cls, conn: sqlite3.Connection, run: str, now: datetime) -> ChangeSet:
        return cls(run=run, now=now, next_number=sequences.current(conn, sequences.PROBLEM) + 1)

    def allocate(self) -> str:
        number = self.next_number
        self.next_number += 1
        return problem_id(number)

    def load(self, found: Iterable[Problem]) -> None:
        """登记从数据库批量读入的问题(按编号升序)；本次已在内存中的以内存为准，已并入别处的不进指纹索引。
        同一指纹有多个问题时取编号最大的(最近建立的)。"""
        for problem in found:
            if problem.id in self.known:
                continue
            self.known[problem.id] = problem
            if problem.extra.get(MERGED_INTO) is None:
                for value in (problem.fingerprint, *problem.extra.get(ALIASES, [])):
                    self.index[value] = problem.id

    def by_fingerprint(self, value: str) -> Problem | None:
        found = self.index.get(value)
        return self.known[found] if found is not None else None

    def put(self, problem: Problem, *, created: bool = False) -> None:
        self.known[problem.id] = problem
        self.touched.add(problem.id)
        if created:
            self.created.append(problem.id)
            self.index[problem.fingerprint] = problem.id

    def alias(self, problem: Problem, value: str) -> None:
        aliases = problem.extra.setdefault(ALIASES, [])
        if value != problem.fingerprint and value not in aliases:
            aliases.append(value)
        self.index[value] = problem.id
        self.put(problem)

    def record(self, problem: Problem, signal: Signal) -> None:
        self.occurred.setdefault(problem.id, []).append(signal)
        self.occurrences.append(Occurrence(
            problem=problem.id, seen_at=signal_time(signal), source=signal.source, run=signal.run,
            commit=signal.commit, evidence=signal_record(signal),
        ))

    def transition(self, problem: Problem, event: status_rules.Event, **context: Any) -> Problem:
        """按状态表转换；不在表中的组合抛 TransitionRejected，整次去重中止，不写入任何东西。"""
        after = status_rules.transition(problem, event)
        before = problem.status
        problem.status = after
        self.put(problem)
        self.transitions.append(Transition(
            problem.id, event.value, before, after, {key: value for key, value in context.items() if value is not None},
            format_iso(self.now),
        ))
        return problem

    def became(self, problem: str) -> str | None:
        """本次最后一次转换后的状态；本次没有转换时为 None。"""
        found = [item.after for item in self.transitions if item.problem == problem and item.after != item.before]
        return found[-1] if found else None


def signal_time(signal: Signal) -> datetime:
    return parse_iso(signal.occurred_at)


def signal_record(signal: Signal) -> dict[str, Any]:
    """写进 occurrences.evidence 的内容：信号的稳定字段与来源给的证据(已脱敏)。运行、时间、commit 已在列中。"""
    return {
        "signal": signal.id, "checkType": signal.check_type, "location": signal.location, "symbol": signal.symbol,
        "message": signal.message, "environment": signal.environment, "severityHint": signal.severity_hint,
        "groupKey": signal.group_key, "deterministic": signal.deterministic, "verified": signal.verified,
        "reproducible": signal.reproducible, "evidence": signal.evidence,
    }


def load_by_fingerprints(conn: sqlite3.Connection, values: Iterable[str]) -> list[Problem]:
    """一次查出指纹或别名命中的问题(不在循环里逐条查库)。"""
    wanted = sorted(set(values))
    if not wanted:
        return []
    marks = ", ".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT * FROM problems WHERE fingerprint IN ({marks}) OR EXISTS "
        f"(SELECT 1 FROM json_each(problems.extra, '$.{ALIASES}') WHERE value IN ({marks})) ORDER BY id",
        [*wanted, *wanted],
    ).fetchall()
    return [problems.TABLE.from_row(row) for row in rows]


def load_where(conn: sqlite3.Connection, column: str, values: Iterable[str]) -> list[Problem]:
    """按某一列的取值批量读问题；column 只来自本包的常量。"""
    wanted = sorted(set(values))
    if not wanted:
        return []
    rows = conn.execute(
        f'SELECT * FROM problems WHERE "{column}" IN ({", ".join("?" for _ in wanted)}) ORDER BY id', wanted
    ).fetchall()
    return [problems.TABLE.from_row(row) for row in rows]
