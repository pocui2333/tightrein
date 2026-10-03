"""第 5 步 状态更新(design 2.8)。

本次出现的问题：再次出现(复现检查失败的记为 regression-check-failed)；已解决的问题只有出现在比解决时更新的版本上才算
回归；已忽略的问题检查恢复条件。本次运行覆盖了位置而没有出现的新发现或持续问题：只在运行的 commit 晚于问题最后
出现的 commit 时累计覆盖运行，满足次数(static 为 1 次)时判为已解决。另检查按日期到期的忽略。
commit 关系由调用方先按 commit_pairs 查询好，以 CommitFacts 传入；关系未知的问题本次不做判定，在 notes 中列出。
待确认的问题由复现确认处理，本步跳过。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from tightrein.domain.commit_facts import CommitFacts
from tightrein.domain.enums import Probe, ProblemEvent, ProblemStatus
from tightrein.domain.ids import parse_sequence
from tightrein.domain.problem import (
    RESOLVABLE,
    Problem,
    ProblemContext,
    count_clean_run,
    ignore_expired,
    is_covered,
    is_regression,
    resolution_ready,
)
from tightrein.domain.run import Run
from tightrein.pipeline.aggregate.changeset import ChangeSet
from tightrein.store.repos import problems

LEFT_TO_REPRODUCE = frozenset({ProblemStatus.PENDING})


def _release(changeset: ChangeSet, problem_id: str) -> str | None:
    """本次出现中最晚一条带 commit 的信号的 commit。"""
    found = [changeset.signals[signal_id] for signal_id in changeset.occurred[problem_id]]
    with_release = [signal for signal in found if signal.release is not None]
    return max(with_release, key=lambda signal: (signal.occurred_at, signal.id)).release if with_release else None


def _occurred(changeset: ChangeSet, conn: sqlite3.Connection) -> list[Problem]:
    changed = {change.problem_id for change in changeset.changes}
    result = []
    for problem_id in sorted(changeset.occurred, key=parse_sequence):
        problem = changeset.problem(conn, problem_id)
        if problem is None or problem_id in changeset.created or problem_id in changed:
            continue
        if problem.status not in LEFT_TO_REPRODUCE:
            result.append(problem)
    return result


def _with_status(changeset: ChangeSet, conn: sqlite3.Connection, statuses: frozenset[ProblemStatus],
                 probe: Probe | None = None) -> list[Problem]:
    found = {problem.id: problem for problem in problems.find(conn, statuses=statuses, probe=probe)}
    found.update({problem.id: problem for problem in changeset.problems.values()})
    return sorted((problem for problem in found.values() if problem.status in statuses
                   and (probe is None or problem.probe is probe) and problem.merged_into is None
                   and problem.id not in changeset.occurred), key=lambda problem: parse_sequence(problem.id))


def _covered(changeset: ChangeSet, conn: sqlite3.Connection, run: Run | None) -> list[Problem]:
    if run is None or run.probe is None:
        return []
    judged = changeset.runs[run.id]
    return [problem for problem in _with_status(changeset, conn, RESOLVABLE, run.probe) if is_covered(problem, judged)]


def commit_pairs(changeset: ChangeSet, conn: sqlite3.Connection, run: Run | None) -> set[tuple[str, str]]:
    """本步需要的 (祖先, 后代) 对。"""
    pairs: set[tuple[str, str]] = set()
    for problem in _occurred(changeset, conn):
        release = _release(changeset, problem.id)
        if problem.status is ProblemStatus.RESOLVED and problem.resolved_release and release:
            pairs.add((problem.resolved_release, release))
        condition = problem.ignore_until
        if condition is not None and condition.new_release and condition.baseline_release and problem.last_seen_release:
            pairs.add((condition.baseline_release, problem.last_seen_release))
    if run is not None and run.target_commit is not None:
        for problem in _covered(changeset, conn, run):
            if problem.last_seen_release is not None:
                pairs.add((problem.last_seen_release, run.target_commit))
    return pairs


def _unknown(changeset: ChangeSet, problem: Problem, judgement: str) -> None:
    changeset.notes.append(f"{problem.id}：commit 的先后关系未知，本次不做{judgement}判定")


def _seen_again(changeset: ChangeSet, problem: Problem, facts: CommitFacts, now: datetime) -> None:
    event = (ProblemEvent.REGRESSION_CHECK_FAILED if problem.id in changeset.regression_hits
             else ProblemEvent.SEEN_AGAIN)
    if problem.status is ProblemStatus.IGNORED:
        expired = ignore_expired(problem, now, facts)
        if expired is None:
            _unknown(changeset, problem, "忽略到期")
        if expired:
            changeset.transition(problem, ProblemEvent.IGNORE_EXPIRED)
            return
    regressed = False
    if problem.status is ProblemStatus.RESOLVED:
        result = is_regression(problem, _release(changeset, problem.id), facts)
        if result is None:
            _unknown(changeset, problem, "回归")
        regressed = bool(result)
    changeset.transition(problem, event, ProblemContext(regressed=regressed, issue_id=problem.issue_id))


def _resolve_covered(changeset: ChangeSet, conn: sqlite3.Connection, run: Run, facts: CommitFacts,
                     covered_runs: int) -> None:
    judged = changeset.runs[run.id]
    for problem in _covered(changeset, conn, run):
        counted = count_clean_run(problem, judged, facts)
        ready = resolution_ready(counted, judged, facts, covered_runs)
        if ready is None:
            _unknown(changeset, problem, "解决")
        if not ready:
            if counted != problem:
                changeset.put_problem(counted)
            continue
        event = (ProblemEvent.RESOLVED_ON_NEW_COMMIT if problem.probe is Probe.STATIC
                 else ProblemEvent.COVERED_RUN_WITHOUT_OCCURRENCE)
        changeset.transition(counted, event, ProblemContext(ready_to_resolve=True, release=judged.target_commit))


def apply(changeset: ChangeSet, conn: sqlite3.Connection, run: Run | None, facts: CommitFacts, now: datetime,
          covered_runs: int) -> None:
    for problem in _occurred(changeset, conn):
        _seen_again(changeset, problem, facts, now)
    if run is not None:
        _resolve_covered(changeset, conn, run, facts, covered_runs)
    for problem in _with_status(changeset, conn, frozenset({ProblemStatus.IGNORED})):
        if problem.ignore_until is not None and problem.ignore_until.until is not None \
                and ignore_expired(problem, now, facts) is True:
            changeset.transition(problem, ProblemEvent.IGNORE_EXPIRED)
