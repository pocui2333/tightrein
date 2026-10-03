"""第 4 步 复现确认(redesign/02-aggregate.md 第 3 节)。

本次新建的待确认问题与此前遗留的待确认问题：api-fuzz 的 not_a_server_error 经注入的重放器重放(目标不可用时保持
待确认，下次重试)；其余首次观察即有效(遗留的待确认问题按当前策略判定，策略为直接有效的在本次转为有效)。
重放未复现的问题保持待确认并标记间歇，之后不再重放；以后某次运行再次出现时转为新(promoted)。
没有提供重放器(--reproduce skip)时，需要重放的问题保持待确认。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

from tightrein.domain.enums import ProblemEvent, ProblemStatus, ReproduceStrategy
from tightrein.domain.problem import Problem
from tightrein.domain.reproduce import judge_replays, strategy
from tightrein.domain.signal import Signal
from tightrein.pipeline.aggregate.changeset import ChangeSet, Reproduction
from tightrein.store.repos import problems, signals

Replayer = Callable[[Signal, int], Sequence[bool | None]]
REPRODUCED = {ProblemEvent.REPRODUCED: True, ProblemEvent.NOT_REPRODUCED: False}


def _latest(changeset: ChangeSet, conn: sqlite3.Connection, problem: Problem) -> Signal | None:
    present = [*signals.get_many(conn, problems.signal_ids(conn, problem.id)),
               *(changeset.signals[signal_id] for signal_id in changeset.occurred.get(problem.id, ()))]
    return max(present, key=lambda signal: (signal.occurred_at, signal.id)) if present else None


def _replay(changeset: ChangeSet, problem: Problem, signal: Signal, replayer: Replayer | None,
            attempts: int) -> None:
    if replayer is None:
        changeset.reproduction[problem.id] = Reproduction(ReproduceStrategy.REPLAY)
        return
    results = list(replayer(signal, attempts))
    event = judge_replays(results, attempts)
    changeset.reproduction[problem.id] = Reproduction(ReproduceStrategy.REPLAY, len(results), REPRODUCED.get(event))
    if event is not None:
        changeset.transition(problem, event)


def _confirm(changeset: ChangeSet, problem: Problem, signal: Signal, replayer: Replayer | None,
             attempts: int) -> None:
    if strategy(problem.probe, signal.check) is ReproduceStrategy.REPLAY:
        _replay(changeset, problem, signal, replayer, attempts)
        return
    changeset.reproduction[problem.id] = Reproduction(ReproduceStrategy.IMMEDIATE, reproduced=True)
    changeset.transition(problem, ProblemEvent.REPRODUCED)


def apply(changeset: ChangeSet, conn: sqlite3.Connection, *, replayer: Replayer | None, attempts: int) -> None:
    for problem_id in changeset.created:
        problem = changeset.problems[problem_id]
        if problem.status is ProblemStatus.PENDING:
            _confirm(changeset, problem, changeset.signals[changeset.occurred[problem_id][0]], replayer, attempts)
    for stored in problems.find(conn, statuses=[ProblemStatus.PENDING]):
        problem = changeset.problems.get(stored.id, stored)
        if problem.id in changeset.created or problem.status is not ProblemStatus.PENDING:
            continue
        if problem.intermittent:
            if problem.id in changeset.occurred:
                changeset.transition(problem, ProblemEvent.PROMOTED)
            continue
        signal = _latest(changeset, conn, problem)
        if signal is not None:
            _confirm(changeset, problem, signal, replayer, attempts)
