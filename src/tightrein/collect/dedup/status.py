"""状态更新：问题状态表与本次的判定(再次出现、回归、已解决、观察期、忽略到期)。

- 状态转换只经 TRANSITIONS 查表，不在表中的组合抛 TransitionRejected，整次去重中止，不会静默写入非法状态；
- commit 先后关系先汇总本次要用的 commit 对、一次查完(CommitFacts)再判定，不在循环中逐个查 git；
  查不到的 commit(本地没同步)视为关系未知，本次不做解决、回归、忽略到期判定，在说明中列出，不会误判；
- 回归只在严格更新的 commit 上判定(是祖先且不是同一个，兄弟分支不算)；
- 已解决：覆盖运行里没出现、并且运行的 commit 比问题最后出现的 commit 更新，才累计一次；
  次数按来源取 resolveCoveredRuns(静态巡检 1 次，其余 3 次)；
- 只有状态为 done 或 partial 的来源运行参与覆盖判定，且只按它实际读到、测到的范围(SourceResult.coverage)：
  被健康检查挡住、失败、跳过的运行不会让问题被判为已解决；
- 判为误报算「判对」：评估判为不成立的问题，之后同一来源覆盖到它的可信运行累计 falsePositiveCleanRuns 次、且其间
  没有指纹(含别名)相同的信号，就把评估结论的实际结果回填为 correct(problems.extra.assess.outcome)；
  被抑制的信号不关联到问题，所以按本次全部信号(含被抑制的)的指纹查，不按关联查(旧 pipeline/learn/steps/outcomes.py)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from tightrein.assess.persist import CORRECT
from tightrein.assess.refute import REFUTED
from tightrein.assess.select import ASSESS, MERGED_INTO
from tightrein.collect.common.signals import SEVERITY_HINTS, Signal
from tightrein.collect.dedup import normalize
from tightrein.protocol.naming import format_iso, parse_iso
from tightrein.store.tables import problems as problem_table
from tightrein.store.tables.problems import Problem

if TYPE_CHECKING:
    from tightrein.collect.dedup.changes import ChangeSet

# problems.extra 中的键
CLEAN_RUNS = "cleanRuns"
RESOLVED_COMMIT = "resolvedCommit"
IGNORE = "ignore"
ROLES = "roles"
SUB_SOURCE = "subSource"
FALSE_POSITIVE_CHECK = "falsePositiveCheck"  # 判为不成立之后的核对：{attempt, cleanRuns, seen}
ALIASES = "aliases"
ROLE_MARK = "#"  # 覆盖条目中接口与角色的分隔


class ProblemStatus(StrEnum):
    PENDING = "pending"  # 待确认，等复现
    WATCHING = "watching"  # 观察中：偶发，未到次数
    NEW = "new"  # 待评估
    ONGOING = "ongoing"  # 已评估且仍出现
    INTERMITTENT = "intermittent"  # 重放未复现
    RESOLVED = "resolved"
    REGRESSED = "regressed"
    MUTED = "muted"  # 已抑制(按恢复条件忽略)
    CLOSED = "closed"  # 评估判为不成立或不修


class Event(StrEnum):
    CONFIRMED = "confirmed"  # 复现确认有效
    WATCH = "watch"  # 有效但偶发，先观察
    NOT_REPRODUCED = "not_reproduced"
    PROMOTED = "promoted"  # 间歇或观察中的问题再出现到次数
    SEEN_AGAIN = "seen_again"
    REGRESSED = "regressed"
    RESOLVED = "resolved"
    IGNORE_EXPIRED = "ignore_expired"


FOR_ASSESS = frozenset({ProblemStatus.NEW, ProblemStatus.REGRESSED})
RESOLVABLE = frozenset({ProblemStatus.NEW, ProblemStatus.ONGOING})
CANDIDATES = (*RESOLVABLE, ProblemStatus.MUTED)  # 没出现也要判定的：覆盖后解决、忽略按日期到期
_ALL = frozenset(ProblemStatus)

# 事件 → (允许的原状态, 新状态；None 为不变)
TRANSITIONS: dict[Event, tuple[frozenset[ProblemStatus], ProblemStatus | None]] = {
    Event.CONFIRMED: (frozenset({ProblemStatus.PENDING}), ProblemStatus.NEW),
    Event.WATCH: (frozenset({ProblemStatus.PENDING}), ProblemStatus.WATCHING),
    Event.NOT_REPRODUCED: (frozenset({ProblemStatus.PENDING}), ProblemStatus.INTERMITTENT),
    Event.PROMOTED: (frozenset({ProblemStatus.INTERMITTENT, ProblemStatus.WATCHING}), ProblemStatus.NEW),
    Event.SEEN_AGAIN: (_ALL, None),
    Event.REGRESSED: (frozenset({ProblemStatus.RESOLVED}), ProblemStatus.REGRESSED),
    Event.RESOLVED: (RESOLVABLE, ProblemStatus.RESOLVED),
    Event.IGNORE_EXPIRED: (frozenset({ProblemStatus.MUTED}), ProblemStatus.NEW),
}


class TransitionRejected(Exception):
    def __init__(self, problem: str, status: str, event: Event) -> None:
        super().__init__(f"问题 {problem}：状态 {status} 不接受事件 {event.value}")
        self.problem = problem


@dataclass(frozen=True)
class IgnoreCondition:
    """忽略(muted)的恢复条件，任一满足即恢复；全部为空表示永久忽略。再出现次数与新版本以忽略时的出现次数与最近 commit 为基准。"""

    until: datetime | None = None
    occurrences: int | None = None
    new_release: bool = False
    severity_escalated: bool = False
    baseline_occurrences: int = 0
    baseline_commit: str | None = None

    @property
    def permanent(self) -> bool:
        return self.until is None and self.occurrences is None and not self.new_release and not self.severity_escalated

    def to_json(self) -> dict[str, Any]:
        return {
            "until": format_iso(self.until) if self.until else None, "occurrences": self.occurrences,
            "newRelease": self.new_release, "severityEscalated": self.severity_escalated,
            "baselineOccurrences": self.baseline_occurrences, "baselineCommit": self.baseline_commit,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> IgnoreCondition:
        until = data.get("until")
        return cls(
            until=parse_iso(until) if until else None, occurrences=data.get("occurrences"),
            new_release=bool(data.get("newRelease")), severity_escalated=bool(data.get("severityEscalated")),
            baseline_occurrences=int(data.get("baselineOccurrences") or 0), baseline_commit=data.get("baselineCommit"),
        )


@dataclass(frozen=True)
class CommitFacts:
    """ancestry[(a, b)] 为真表示 a 是 b 的祖先；查不到的对不在其中，视为未知。"""

    ancestry: dict[tuple[str, str], bool] = field(default_factory=dict)

    def is_newer(self, commit: str | None, than: str | None) -> bool | None:
        """commit 是否严格晚于 than；任一方为空或关系未知时为 None。"""
        if commit is None or than is None:
            return None
        if commit == than:
            return False
        return self.ancestry.get((than, commit))


@dataclass(frozen=True)
class Coverage:
    """一次来源运行真正测到的范围(SourceResult.coverage)与那次运行所对应的 commit。

    条目写法：接口为 `方法 路由模板`(全部角色都测到)或 `方法 路由模板#角色`；其余条目是代码文件路径或子来源名。
    """

    commit: str | None = None
    items: frozenset[str] = frozenset()
    endpoints: frozenset[tuple[str, str, str | None]] = frozenset()

    @classmethod
    def from_items(cls, items: Iterable[str], commit: str | None) -> Coverage:
        plain, endpoints = set(), set()
        for item in items:
            method, _, rest = item.partition(" ")
            if method.upper() in normalize.HTTP_METHODS and rest.startswith("/"):
                route, _, role = rest.partition(ROLE_MARK)
                endpoints.add((method.upper(), route, role or None))
            else:
                plain.add(item)
        return cls(commit, frozenset(plain), frozenset(endpoints))


@dataclass(frozen=True)
class Params:
    covered_runs: Mapping[str, int]  # 来源 → 判为已解决所需的覆盖次数；`*` 为其余来源
    watch_window: timedelta
    watch_occurrences: int
    false_positive_clean_runs: int  # 判为不成立之后覆盖而未出现几次即回填判对


@dataclass
class Plan:
    """本步要判定的问题与要查的 commit 对(先汇总再一次查完)。"""

    occurred: list[Problem] = field(default_factory=list)
    covered: list[tuple[Problem, Coverage]] = field(default_factory=list)
    muted: list[Problem] = field(default_factory=list)
    pairs: set[tuple[str, str]] = field(default_factory=set)


def transition(problem: Problem, event: Event) -> str:
    allowed, target = TRANSITIONS[event]
    if problem.status not in allowed:
        raise TransitionRejected(problem.id, problem.status, event)
    return problem.status if target is None else target.value


def ignore_condition(problem: Problem, *, until: datetime | None = None, occurrences: int | None = None,
                     new_release: bool = False, severity_escalated: bool = False) -> IgnoreCondition:
    """以问题当前的出现次数与最近 commit 为基准生成恢复条件(评估判为暂不修时调用)。"""
    return IgnoreCondition(until, occurrences, new_release, severity_escalated, problem.count, problem.last_commit)


def ignore_expired(problem: Problem, now: datetime, facts: CommitFacts, *, escalated: bool = False) -> bool | None:
    """任一条件成立即为 True；其余不成立而有条件未知时为 None。escalated 由调用方给出(去重按 escalated 判断)。"""
    data = problem.extra.get(IGNORE)
    if problem.status != ProblemStatus.MUTED or data is None:
        return False
    condition = IgnoreCondition.from_json(data)
    results: list[bool | None] = []
    if condition.until is not None:
        results.append(now >= condition.until)
    if condition.occurrences is not None:
        results.append(problem.count - condition.baseline_occurrences >= condition.occurrences)
    if condition.new_release:
        results.append(facts.is_newer(problem.last_commit, condition.baseline_commit))
    if condition.severity_escalated:
        results.append(escalated)
    if any(result is True for result in results):
        return True
    return None if any(result is None for result in results) else False


def escalated(problem: Problem, signals: Iterable[Signal]) -> bool:
    """本次信号带的严重度提示比评估定的严重度(problems.extra.severity)更高即为升级；没评估过严重度的不算。"""
    current = problem.extra.get("severity")
    if current not in SEVERITY_HINTS:
        return False
    return any(signal.severity_hint in SEVERITY_HINTS
               and SEVERITY_HINTS.index(signal.severity_hint) < SEVERITY_HINTS.index(current) for signal in signals)


def query_facts(pairs: Iterable[tuple[str, str]], is_ancestor: Callable[[str, str], bool | None]) -> CommitFacts:
    """pairs 为 (祖先, 后代)；同一对只查一次，查不到的(None)不记，视为未知。"""
    found: dict[tuple[str, str], bool] = {}
    for ancestor, descendant in sorted(set(pairs)):
        if ancestor == descendant:
            continue
        known = is_ancestor(ancestor, descendant)
        if known is not None:
            found[(ancestor, descendant)] = known
    return CommitFacts(found)


def is_covered(problem: Problem, source: str, coverage: Coverage) -> bool:
    """本次运行是否真正测到了问题所在的位置：必须同一来源；有子来源的(平台、项目探针)看本次是否读到了该子来源；
    接口看问题涉及的每个角色都测到(没有角色的接口任一角色测到即可)；代码位置看文件是否在扫描范围内。
    任务外发现的覆盖范围为空，永远不会因「覆盖运行里没出现」被判为已解决。"""
    if problem.source != source:
        return False
    sub_source = problem.extra.get(SUB_SOURCE)
    if sub_source is not None:
        return sub_source in coverage.items
    if problem.location is None:
        return False
    method, _, route = problem.location.partition(" ")
    if method in normalize.HTTP_METHODS and route.startswith("/"):
        tested = {(item_method, item_route) for item_method, item_route, role in coverage.endpoints if role is None}
        if (method, route) in tested:
            return True
        roles: list[str] = list(problem.extra.get(ROLES) or [])
        if not roles:
            return any((item_method, item_route) == (method, route)
                       for item_method, item_route, _ in coverage.endpoints)
        return all((method, route, role) in coverage.endpoints for role in roles)
    return problem.location.partition(":")[0] in coverage.items


def count_clean_run(problem: Problem, coverage: Coverage, facts: CommitFacts) -> bool:
    """覆盖运行中没有出现：只有运行的 commit 晚于问题最后出现的 commit 时才累计。返回是否累计了。"""
    if facts.is_newer(coverage.commit, problem.last_commit) is not True:
        return False
    problem.extra[CLEAN_RUNS] = int(problem.extra.get(CLEAN_RUNS, 0)) + 1
    return True


def resolution_ready(problem: Problem, coverage: Coverage, facts: CommitFacts, required: int) -> bool | None:
    if problem.status not in RESOLVABLE:
        return False
    newer = facts.is_newer(coverage.commit, problem.last_commit)
    if newer is None:
        return None
    return newer and int(problem.extra.get(CLEAN_RUNS, 0)) >= required


def is_regression(problem: Problem, commit: str | None, facts: CommitFacts) -> bool | None:
    """已解决的问题出现在晚于解决时 commit 的版本上才算回归；旧版本上的迟到信号只累计。"""
    if problem.status != ProblemStatus.RESOLVED:
        return False
    return facts.is_newer(commit, problem.extra.get(RESOLVED_COMMIT))


def plan(changeset: ChangeSet, coverages: Mapping[str, Coverage]) -> Plan:
    """coverages 为本次 done 或 partial 的来源运行各自的覆盖范围；调用前已把候选问题批量读进变更集。"""
    result = Plan()
    decided = {item.problem for item in changeset.transitions}
    for problem_id in sorted(changeset.occurred):
        problem = changeset.known[problem_id]
        if problem_id in changeset.created or problem_id in decided or problem.status == ProblemStatus.PENDING:
            continue
        result.occurred.append(problem)
        commit = latest_commit(changeset, problem_id)
        if problem.status == ProblemStatus.RESOLVED and problem.extra.get(RESOLVED_COMMIT) and commit:
            result.pairs.add((problem.extra[RESOLVED_COMMIT], commit))
    for problem in sorted(changeset.known.values(), key=lambda item: item.id):
        if problem.id in changeset.occurred:
            continue
        coverage = coverages.get(problem.source)
        if problem.status in RESOLVABLE and coverage is not None and is_covered(problem, problem.source, coverage):
            result.covered.append((problem, coverage))
            if problem.last_commit and coverage.commit:
                result.pairs.add((problem.last_commit, coverage.commit))
        if problem.status == ProblemStatus.MUTED:
            result.muted.append(problem)
    for problem in [*result.occurred, *result.muted]:
        condition = problem.extra.get(IGNORE) or {}
        if problem.status == ProblemStatus.MUTED and condition.get("newRelease") and condition.get("baselineCommit") \
                and problem.last_commit:
            result.pairs.add((condition["baselineCommit"], problem.last_commit))
    return result


def apply(changeset: ChangeSet, conn: sqlite3.Connection, work: Plan, facts: CommitFacts, params: Params) -> None:
    windows = _window_counts(conn, changeset, [item.id for item in work.occurred
                                               if item.status == ProblemStatus.WATCHING], params.watch_window)
    for problem in work.occurred:
        _seen_again(changeset, problem, facts, windows.get(problem.id, 0), params)
    for problem, coverage in work.covered:
        _resolve(changeset, problem, coverage, facts, params)
    for problem in work.muted:
        if problem.status == ProblemStatus.MUTED and ignore_expired(problem, changeset.now, facts) is True:
            changeset.transition(problem, Event.IGNORE_EXPIRED)


def latest_commit(changeset: ChangeSet, problem_id: str) -> str | None:
    """本次出现中最晚一条带 commit 的信号的 commit。"""
    found = [signal for signal in changeset.occurred.get(problem_id, []) if signal.commit is not None]
    return max(found, key=lambda signal: (signal.occurred_at, signal.id)).commit if found else None


def required_runs(params: Params, source: str) -> int:
    return int(params.covered_runs.get(source, params.covered_runs["*"]))


def _seen_again(changeset: ChangeSet, problem: Problem, facts: CommitFacts, in_window: int, params: Params) -> None:
    if problem.status == ProblemStatus.MUTED:
        expired = ignore_expired(problem, changeset.now, facts,
                                 escalated=escalated(problem, changeset.occurred.get(problem.id, [])))
        if expired is None:
            _unknown(changeset, problem, "忽略到期")
        if expired:
            changeset.transition(problem, Event.IGNORE_EXPIRED)
            return
    if problem.status == ProblemStatus.RESOLVED:
        commit = latest_commit(changeset, problem.id)
        regressed = is_regression(problem, commit, facts)
        if regressed is None:
            _unknown(changeset, problem, "回归")
        if regressed:
            changeset.transition(problem, Event.REGRESSED, commit=commit, issue=problem.issue,
                                 regressionCheck=True if problem.id in changeset.regression_checks else None)
            if problem.issue is not None:
                changeset.reopened.append((problem.issue, problem.id))
            return
    if problem.status == ProblemStatus.INTERMITTENT:
        changeset.transition(problem, Event.PROMOTED)
        return
    if problem.status == ProblemStatus.WATCHING and in_window >= params.watch_occurrences:
        changeset.transition(problem, Event.PROMOTED, occurrences=in_window)
        return
    changeset.transition(problem, Event.SEEN_AGAIN)


def _resolve(changeset: ChangeSet, problem: Problem, coverage: Coverage, facts: CommitFacts, params: Params) -> None:
    if count_clean_run(problem, coverage, facts):
        changeset.put(problem)
    ready = resolution_ready(problem, coverage, facts, required_runs(params, problem.source))
    if ready is None and coverage.commit and problem.last_commit:  # 没有版本信息(没配部署来源)不算未知，不刷说明
        _unknown(changeset, problem, "解决")
    if ready:
        problem.extra[RESOLVED_COMMIT] = coverage.commit
        changeset.transition(problem, Event.RESOLVED, commit=coverage.commit)


def _window_counts(conn: sqlite3.Connection, changeset: ChangeSet, problem_ids: list[str],
                   window: timedelta) -> dict[str, int]:
    """观察期内的出现次数：库中已有的加本次的，一次查完。"""
    if not problem_ids:
        return {}
    since = changeset.now - window
    rows = conn.execute(
        f"SELECT problem, COUNT(*) FROM occurrences WHERE problem IN ({', '.join('?' for _ in problem_ids)}) "
        "AND seen_at >= ? GROUP BY problem", [*problem_ids, format_iso(since)],
    ).fetchall()
    counts = {row[0]: int(row[1]) for row in rows}
    for problem_id in problem_ids:
        counts[problem_id] = counts.get(problem_id, 0) + sum(
            1 for signal in changeset.occurred[problem_id] if parse_iso(signal.occurred_at) >= since)
    return counts


def refuted_problems(conn: sqlite3.Connection) -> list[Problem]:
    """评估判为不成立、结论还没有实际结果的问题(已关闭，未并入别处)，一次查完。"""
    rows = conn.execute(
        "SELECT * FROM problems WHERE status = ? AND json_extract(extra, '$.verdict') = ? "
        f"AND json_extract(extra, '$.{ASSESS}.outcome') IS NULL AND json_extract(extra, '$.{MERGED_INTO}') IS NULL "
        "ORDER BY id", (ProblemStatus.CLOSED.value, REFUTED),
    ).fetchall()
    return [problem_table.TABLE.from_row(row) for row in rows]


def false_positive_outcomes(changeset: ChangeSet, candidates: Iterable[Problem], fingerprints: Iterable[str],
                            coverages: Mapping[str, Coverage], params: Params) -> list[str]:
    """判为不成立的问题：本次有指纹(含别名)相同的信号(含被抑制的)即不再算判对；没有且本次可信运行覆盖到它时累计
    一次，到 falsePositiveCleanRuns 次回填 correct。同一问题重新评估后(attempt 变了)重新计数。返回回填的问题编号。"""
    seen = set(fingerprints)
    filled = []
    for candidate in candidates:
        problem = changeset.known.get(candidate.id, candidate)
        record = dict(problem.extra.get(ASSESS) or {})
        if problem.status != ProblemStatus.CLOSED or problem.extra.get("verdict") != REFUTED or record.get("outcome"):
            continue
        attempt = int(record.get("attempt") or 1)
        check = dict(problem.extra.get(FALSE_POSITIVE_CHECK) or {})
        if check.get("attempt") != attempt:
            check = {"attempt": attempt, "cleanRuns": 0, "seen": False}
        if check["seen"]:
            continue
        prints = {problem.fingerprint, *problem.extra.get(ALIASES, [])}
        coverage = coverages.get(problem.source)
        if prints & seen or problem.id in changeset.occurred:
            check["seen"] = True
        elif coverage is not None and is_covered(problem, problem.source, coverage):
            check["cleanRuns"] = int(check["cleanRuns"]) + 1
        else:
            continue
        problem.extra[FALSE_POSITIVE_CHECK] = check
        if not check["seen"] and check["cleanRuns"] >= params.false_positive_clean_runs:
            problem.extra[ASSESS] = {**record, "outcome": CORRECT, "outcomeAt": format_iso(changeset.now),
                                     "outcomeDetail": f"判为不成立后被覆盖 {check['cleanRuns']} 次未再出现"}
            filled.append(problem.id)
        changeset.put(problem)
    return filled


def _unknown(changeset: ChangeSet, problem: Problem, judgement: str) -> None:
    changeset.notes.append(f"{problem.id}：commit 的先后关系未知，本次不做{judgement}判定")
