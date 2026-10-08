"""选取待评估的问题并排序；把互不相关的问题分组，组与组之间并行评估。

- 每个问题只评估一次：状态为 new 或 regressed(进入这两个状态即说明有新情况)才评估；评估后状态会离开这两个状态，
  只有转人工的保持原状态并带 manual 标记，不再自动评估。用户请求重新评估(retriage 标记)时不受此限。
  避免同一问题被反复烧 token；
- 已并入其他问题的拒绝并写明并入了哪个；muted、closed、resolved 的要先 reopen；
- 排序分五档：预估 P0(越权)→ 服务端报错 → 回归 → 其他运行时问题 → 静态、任务外发现与响应过慢；同一档内按末次
  出现时间倒序。预估只用来排序，不当结论用；
- 相关：位置相同或位置所在的文件相同，同组内按顺序评估(后一个查重时能看到前一个的结论)。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tightrein.store.tables import problems
from tightrein.store.tables.problems import Problem

ASSESS = "assess"  # problems.extra 中评估写的部分
MERGED_INTO = "mergedInto"
FOR_ASSESS = frozenset({"new", "regressed"})
RETRIAGEABLE = frozenset({"new", "ongoing", "regressed", "watching", "intermittent"})
SERVER_ERRORS = frozenset({"server_error", "error"})
LOW_BAND = frozenset({"static", "latency"})
ALREADY = "已评估，重新评估请用 tightrein problem retriage"


@dataclass(frozen=True)
class Selection:
    chosen: list[Problem] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


def assessed(problem: Problem) -> dict[str, Any]:
    return dict(problem.extra.get(ASSESS) or {})


def eligible(problem: Problem) -> bool:
    if problem.extra.get(MERGED_INTO) is not None:
        return False
    record = assessed(problem)
    if record.get("retriage") and problem.status in RETRIAGEABLE:
        return True
    return problem.status in FOR_ASSESS and not record.get("manual")


def rejection(conn: sqlite3.Connection, problem_id: str, *, retriage: bool = False) -> tuple[Problem | None, str | None]:
    """指定编号的问题能否评估：返回问题与不能评估的原因。retriage 时不看是否已评估。"""
    problem = problems.get(conn, problem_id)
    if problem is None:
        return None, f"{problem_id} 不存在"
    merged = problem.extra.get(MERGED_INTO)
    if merged is not None:
        return problem, f"{problem_id} 已并入 {merged}，请针对 {merged} 操作"
    allowed = RETRIAGEABLE if retriage else FOR_ASSESS
    if problem.status not in allowed:
        return problem, (f"{problem_id} 状态为 {problem.status}，只能评估 {'、'.join(sorted(allowed))} 的问题；"
                         "已忽略或已关闭的先执行 tightrein problem reopen")
    if not retriage and not eligible(problem):
        return problem, f"{problem_id} {ALREADY}"
    return problem, None


def choose(conn: sqlite3.Connection, limit: int, problem_ids: Sequence[str] = (), *,
           retriage: bool = False) -> Selection:
    if not problem_ids:
        return Selection(pending(conn)[:limit])
    selection = Selection()
    for problem_id in problem_ids:
        problem, reason = rejection(conn, problem_id, retriage=retriage)
        if problem is None or reason is not None:
            selection.rejected.append((problem_id, reason or f"{problem_id} 不存在"))
        else:
            selection.chosen.append(problem)
    selection.chosen[:] = order(conn, selection.chosen)[:limit]
    return selection


def pending(conn: sqlite3.Connection) -> list[Problem]:
    found = [problem for status in sorted(RETRIAGEABLE) for problem in problems.find(conn, status=status)]
    return order(conn, [problem for problem in found if eligible(problem)])


def order(conn: sqlite3.Connection, found: Sequence[Problem]) -> list[Problem]:
    hints = latest_hints(conn, [problem.id for problem in found])
    keyed = sorted(found, key=lambda problem: (band(problem, hints.get(problem.id)), -problem.last_seen.timestamp(),
                                               problem.id))
    return list(keyed)


def band(problem: Problem, severity_hint: str | None) -> int:
    if severity_hint == "P0":
        return 0
    kind = problem.check_type.partition(":")[0]
    if kind in SERVER_ERRORS:
        return 1
    if problem.status == "regressed":
        return 2
    if kind in LOW_BAND or problem.check_type.startswith("incidental") or problem.source == "collect.static":
        return 4
    return 3


def latest_hints(conn: sqlite3.Connection, problem_ids: Iterable[str]) -> dict[str, str | None]:
    """各问题最近一次出现的严重度预估；一次查完，不在循环里逐条查库。"""
    wanted = sorted(set(problem_ids))
    if not wanted:
        return {}
    marks = ", ".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT problem, evidence FROM occurrences WHERE id IN "
        f"(SELECT MAX(id) FROM occurrences WHERE problem IN ({marks}) GROUP BY problem)", wanted,
    ).fetchall()
    return {row["problem"]: json.loads(row["evidence"]).get("severityHint") for row in rows}


def related_groups(found: Sequence[Problem]) -> list[list[Problem]]:
    """位置或位置所在文件相同的问题归为一组(并查集)，保持原有顺序；组与组之间互不相关。"""
    parent = list(range(len(found)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owner: dict[str, int] = {}
    for index, problem in enumerate(found):
        for key in _keys(problem):
            if key in owner:
                parent[root(index)] = root(owner[key])
            else:
                owner[key] = index
    groups: dict[int, list[Problem]] = {}
    for index, problem in enumerate(found):
        groups.setdefault(root(index), []).append(problem)
    return list(groups.values())


def _keys(problem: Problem) -> set[str]:
    if not problem.location:
        return set()
    return {problem.location, problem.location.partition(":")[0]}
