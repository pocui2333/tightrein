"""查重：程序先比，明显重复或明显不重复的直接定，拿不准才调模型(assess.dedup)。

- 候选由程序从数据库取：未关闭的 Issue，加上最近 N 天评估过、没被合并、还没建 Issue 的问题；
- 程序比对三样：指纹(同指纹的采集时已合并)、代码位置(位置相同、位置所在文件或根因文件有交集)、标题相似度
  (difflib 的比例)。位置相同且标题相似度不低于 sameTitle 的直接判为重复；位置与文件都不相交且标题相似度低于
  differentTitle 的不算候选；剩下的按时间取前几个交给模型，没有候选就不调用模型；
- 模型判为同一根因时由程序核对：target 必须是给出的候选之一，依据位置必须真实存在，不合格就带原因重做；
  仍不合格或调用失败时按「不同根因」继续评估(错误合并会吞掉真问题)；
- target 是 Issue 时并入它的第一个问题。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.assess.checks import Snapshot
from tightrein.assess.claims import Claim
from tightrein.assess.prompts import dedup as prompt
from tightrein.assess.prompts.common import Asked, ask
from tightrein.assess.prompts.dedup import ISSUE, PROBLEM, Candidate
from tightrein.assess.select import ASSESS, MERGED_INTO
from tightrein.protocol.naming import format_iso
from tightrein.store.tables import issues, problems
from tightrein.store.tables.problems import Problem

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

CLOSED_ISSUES = frozenset({"done", "cancelled"})
PROGRAM = "program"
MODEL = "model"


@dataclass(frozen=True)
class Params:
    days: int
    limit: int
    same_title: float
    different_title: float
    retries: int

    @classmethod
    def from_settings(cls, section: Mapping[str, Any], retries: int) -> Params:
        dedup = section["dedup"]
        return cls(int(dedup["candidateDays"]), int(dedup["candidates"]), float(dedup["sameTitle"]),
                   float(dedup["differentTitle"]), retries)


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    same_location: bool
    file_hit: bool
    similarity: float
    updated: str


@dataclass
class DedupOutcome:
    target: str | None = None  # 并入的问题编号
    decided_by: str | None = None
    note: str | None = None
    asked: list[Asked] = field(default_factory=list)


def candidates(conn: sqlite3.Connection, problem: Problem, files: Sequence[str], now: datetime,
               days: int) -> list[Scored]:
    """全部候选与程序比对的结果(还没按门槛筛)。"""
    wanted = {_file(problem.location), *files} - {None}
    found: list[Scored] = []
    open_issues = [issue for issue in issues.find(conn) if issue.status not in CLOSED_ISSUES]
    linked = _problems(conn, {pid for issue in open_issues for pid in issue.extra.get("problems") or []})
    for issue in open_issues:
        members = [linked[pid] for pid in issue.extra.get("problems") or [] if pid in linked]
        if problem.id in (issue.extra.get("problems") or []):
            continue
        causes = tuple(f"{cause['file']}:{cause['line']}" for cause in issue.extra.get("rootCauses") or [])
        locations = {member.location for member in members if member.location}
        candidate = Candidate(issue.id, ISSUE, issue.title, next(iter(sorted(locations)), None), causes, issue.status)
        history = issue.extra.get("history") or [{}]
        found.append(_score(candidate, problem, wanted, locations, causes, str(history[-1].get("at") or "")))
    since = format_iso(now - timedelta(days=days))
    rows = conn.execute(
        "SELECT * FROM problems WHERE id != ? AND json_extract(extra, '$.assess.at') >= ? "
        "AND json_extract(extra, '$.mergedInto') IS NULL AND issue IS NULL", (problem.id, since),
    ).fetchall()
    for row in rows:
        other = problems.TABLE.from_row(row)
        record = other.extra.get(ASSESS) or {}
        causes = tuple(f"{cause['file']}:{cause['line']}" for cause in record.get("rootCauses") or [])
        candidate = Candidate(other.id, PROBLEM, other.title, other.location, causes,
                              str(record.get("destination") or other.status))
        locations = {other.location} if other.location else set()
        found.append(_score(candidate, problem, wanted, locations, causes, str(record.get("at"))))
    return found


def screen(scored: Sequence[Scored], params: Params) -> tuple[Candidate | None, list[Candidate]]:
    """程序的判断：(明显重复的目标, 拿不准、要交给模型的候选)。"""
    obvious = [item for item in scored if item.same_location and item.similarity >= params.same_title]
    if obvious:
        return max(obvious, key=lambda item: (item.similarity, item.updated)).candidate, []
    unsure = [item for item in scored
              if item.same_location or item.file_hit or item.similarity >= params.different_title]
    unsure.sort(key=lambda item: (item.updated, item.candidate.id), reverse=True)
    return None, [item.candidate for item in unsure[:params.limit]]


def decide(runtime: Runtime, problem: Problem, claim: Claim, files: Sequence[str], snapshot: Snapshot,
           params: Params) -> DedupOutcome:
    outcome = DedupOutcome()
    scored = candidates(runtime.conn, problem, files, runtime.clock.now(), params.days)
    obvious, unsure = screen(scored, params)
    if obvious is not None:
        outcome.target, outcome.decided_by = merge_target(runtime.conn, obvious), PROGRAM
        outcome.note = f"与 {obvious.id} 位置相同、标题相近，程序判为同一问题"
        return outcome
    if not unsure:
        return outcome
    feedback: list[str] = []
    for attempt in range(1, params.retries + 2):
        asked = ask(runtime, prompt.POINT, prompt.variables(claim, unsure, feedback), subject=problem.id,
                    workdir=snapshot.root, round=attempt)
        outcome.asked.append(asked)
        if not asked.ok or asked.output is None:
            feedback = [f"上一次{asked.failure}"]
            continue
        feedback = check(asked.output, unsure, snapshot)
        if not feedback:
            if asked.output["sameRootCause"]:
                target = next(item for item in unsure if item.id == asked.output["target"])
                outcome.target, outcome.decided_by = merge_target(runtime.conn, target), MODEL
                outcome.note = f"与 {target.id} 同一根因：{asked.output['reason']}"
            return outcome
    outcome.note = "查重没有得到合格的判断，按不同根因继续评估" + (f"：{'；'.join(feedback)}" if feedback else "")
    return outcome


def check(output: Mapping[str, Any], found: Sequence[Candidate], snapshot: Snapshot) -> list[str]:
    if not output["sameRootCause"]:
        return []
    reasons = []
    if output.get("target") not in {item.id for item in found}:
        reasons.append(f"target {output.get('target')} 不在候选中，只能是 {'、'.join(item.id for item in found)}")
    evidence = list(output.get("evidence") or [])
    if not evidence:
        reasons.append("判为同一根因时必须给出依据位置 evidence")
    reasons += [f"依据位置不合格：{problem}" for problem in (snapshot.problem(item) for item in evidence) if problem]
    return reasons


def merge_target(conn: sqlite3.Connection, candidate: Candidate) -> str | None:
    """候选对应的并入目标问题；Issue 取它的第一个问题(没有问题的用户需求不能并入)。"""
    if candidate.kind == PROBLEM:
        return candidate.id
    issue = issues.get(conn, candidate.id)
    members = list(issue.extra.get("problems") or []) if issue is not None else []
    return members[0] if members else None


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.lower(), right.lower()).ratio()


def _score(candidate: Candidate, problem: Problem, wanted: set[str | None], locations: set[str],
           causes: Sequence[str], updated: str) -> Scored:
    files = {_file(location) for location in locations} | {_file(cause) for cause in causes}
    return Scored(candidate, problem.location is not None and problem.location in locations,
                  bool((files - {None}) & wanted), similarity(problem.title, candidate.title), updated)


def _file(location: str | None) -> str | None:
    """「文件:行号」中的文件；路由(带空格)与页面不算文件。"""
    if not location or " " in location:
        return None
    file = location.partition(":")[0]
    return file if "." in Path(file).name else None


def _problems(conn: sqlite3.Connection, ids: set[str]) -> dict[str, Problem]:
    if not ids:
        return {}
    wanted = sorted(ids)
    rows = conn.execute(f"SELECT * FROM problems WHERE id IN ({', '.join('?' for _ in wanted)})", wanted).fetchall()
    found = {row["id"]: problems.TABLE.from_row(row) for row in rows}
    return {pid: problem for pid, problem in found.items() if problem.extra.get(MERGED_INTO) is None}
