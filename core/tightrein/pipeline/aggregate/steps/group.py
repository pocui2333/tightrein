"""第 3 步 指纹与归并(redesign/02-aggregate.md 第 1 节)：平台来源用平台分组编号，其余按逻辑位置计算；
按指纹(含合并产生的别名)找到问题则累计出现，否则新建待确认的问题。

回归信号不计算指纹，直接归到 context.targetFingerprints 对应的问题，并记下该问题本次有复现检查失败。
整体重放时新问题的编号按 ChangeSet.inheritance 继承。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace

from tightrein.domain.enums import ProblemEvent, SignalAggregateState
from tightrein.domain.fingerprint import CURRENT_VERSION, REGRESSION_CHECK, fingerprint
from tightrein.domain.problem import Problem, apply_occurrence, new_problem
from tightrein.domain.signal import Signal
from tightrein.pipeline.aggregate.changeset import ChangeSet


def _regression_target(changeset: ChangeSet, conn: sqlite3.Connection, signal: Signal) -> Problem | None:
    for value in signal.ctx("targetFingerprints") or []:
        problem = changeset.by_fingerprint(conn, value)
        if problem is not None:
            return problem
    return None


def _create(changeset: ChangeSet, signal: Signal, value: str, title_length: int) -> str:
    """新建问题；整体重放时按 Inheritance 继承旧编号与 Issue 编号，拆出的问题写 rebuilt 事件。"""
    inheritance = changeset.inheritance
    preferred, split = inheritance.for_signal(signal.id) if inheritance is not None else (None, False)
    created = new_problem(changeset.allocate(preferred), signal, value, CURRENT_VERSION, title_length)
    if inheritance is not None and preferred is not None:
        created = replace(created, issue_id=inheritance.issue_ids.get(preferred))
    changeset.put_problem(created, created=True)
    if split:
        changeset.transition(created, ProblemEvent.REBUILT)
    return created.id


def apply(changeset: ChangeSet, conn: sqlite3.Connection, signals: Sequence[Signal], title_length: int) -> None:
    """title_length 为新问题标题的最大长度(runtime.aggregate.titleMaxChars)。"""
    for signal in signals:
        if signal.check == REGRESSION_CHECK:
            done = replace(signal, aggregate_state=SignalAggregateState.DONE)
            changeset.put_signal(done)
            target = _regression_target(changeset, conn, done)
            if target is None:
                changeset.notes.append(f"回归信号 {signal.id} 的目标指纹没有对应的问题")
                continue
            changeset.put_problem(apply_occurrence(target, done))
            changeset.regression_hits.add(target.id)
            problem_id = target.id
        else:
            value = fingerprint(signal, CURRENT_VERSION)
            if value is None:
                raise ValueError(f"信号 {signal.id} 没有指纹")
            done = replace(signal, fingerprint=value, aggregate_state=SignalAggregateState.DONE)
            changeset.put_signal(done)
            existing = changeset.by_fingerprint(conn, value)
            if existing is None:
                problem_id = _create(changeset, done, value, title_length)
            else:
                changeset.put_problem(apply_occurrence(existing, done))
                problem_id = existing.id
        changeset.record_occurrence(problem_id, signal.id)
        changeset.current.grouped += 1
