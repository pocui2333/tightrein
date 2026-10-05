"""整体重放(design 2.2，architecture/05 3.6)：指纹规则版本变化或已聚合的运行被重新解析之后，从信号重建全部问题。

1. 正常模式先把数据库复制到 data/archive/tightrein-<时间>.db。
2. 读出现有问题的编号、指纹、信号、Issue 编号，复现确认的结果，以及需要重新施加的事件(人工操作与分诊、Issue 关闭
   等其他模块写入的事件，按 detail.context 还原上下文)。
3. 一个事务内清空问题相关的表，复位 collect 运行与信号的聚合字段，按运行的开始时间逐个执行归并的各步；每个运行之前
   先施加时间不晚于它开始时间的事件，全部运行之后施加其余事件。复现确认不发起网络请求，使用读出的结果，没有记录的
   保持待确认。新问题按 Inheritance 继承编号与 Issue 编号。文件副作用(Issue 与抑制规则)已在原先执行过，不再执行。
4. 写运行级交接文档，列出编号的继承、拆分与合并。任何一个运行失败时整个事务回滚，数据库保持重放前的状态。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tightrein.domain.enums import ProblemEvent
from tightrein.domain.ids import parse_sequence
from tightrein.domain.run import Run
from tightrein.domain.signal import Signal
from tightrein.pipeline.aggregate.changeset import (
    CONTEXT_KEY,
    ChangeSet,
    Inheritance,
    TransitionRejected,
    context_from_dict,
)
from tightrein.pipeline.aggregate.steps import apply, select
from tightrein.pipeline.aggregate.steps.reproduce import Replayer
from tightrein.store.db import transaction
from tightrein.store.repos import problem_events, problems, runs, signals
from tightrein.store.repos.problem_events import OPERATION_USER, ProblemEventRecord
from tightrein.store.repos.problems import MergeRejected

if TYPE_CHECKING:
    from tightrein.pipeline.aggregate.service import AggregateResult, AggregateService, Rules

REAPPLIED = frozenset({ProblemEvent.TRIAGED, ProblemEvent.OVERRIDDEN, ProblemEvent.ISSUE_CLOSED,
                       ProblemEvent.RETRIAGE_REQUESTED})
REPRODUCTION = {ProblemEvent.REPRODUCED: True, ProblemEvent.NOT_REPRODUCED: False}


class RebuildAborted(Exception):
    """重放中某个运行失败，整个事务回滚。"""


@dataclass(frozen=True)
class Snapshot:
    signals_of: dict[str, list[str]]
    issue_ids: dict[str, str]
    reproduced: dict[str, bool]
    events: list[ProblemEventRecord]

    @classmethod
    def read(cls, conn: sqlite3.Connection) -> Snapshot:
        """信号的原属问题按信号自身的指纹确定，合并前归入被并入问题的信号仍算它的。"""
        found = problems.find(conn)
        by_fingerprint = {problem.fingerprint: problem.id for problem in found}
        signals_of: dict[str, list[str]] = defaultdict(list)
        for problem in found:
            for signal in signals.get_many(conn, problems.signal_ids(conn, problem.id)):
                owner = by_fingerprint.get(signal.fingerprint, problem.id) if signal.fingerprint else problem.id
                signals_of[owner].append(signal.id)
        events = [event for problem in found for event in problem_events.for_problem(conn, problem.id)]
        ordered = sorted(events, key=lambda item: (item.at, item.id or 0))
        reproduced = {event.problem_id: REPRODUCTION[event.event] for event in ordered if event.event in REPRODUCTION}
        return cls(
            signals_of=dict(signals_of),
            issue_ids={problem.id: problem.issue_id for problem in found if problem.issue_id is not None},
            reproduced=reproduced,
            events=[event for event in ordered if event.operation == OPERATION_USER or event.event in REAPPLIED],
        )

    def inheritance(self) -> Inheritance:
        owners = {signal_id: problem_id for problem_id, ids in self.signals_of.items() for signal_id in ids}
        return Inheritance(owners, dict(self.issue_ids))

    def replayer(self, inheritance: Inheritance) -> Replayer:
        """按信号原先所属问题的复现确认结果回答，没有记录时为「无法执行」，问题保持待确认。"""
        def replay(signal: Signal, attempts: int) -> list[bool | None]:
            result = self.reproduced.get(inheritance.owners.get(signal.id, ""))
            return [result] * attempts

        return replay


def _report(snapshot: Snapshot, conn: sqlite3.Connection) -> dict[str, Any]:
    """编号的继承、拆分与合并：按新旧问题的信号重合关系列出。"""
    owners = {signal_id: old for old, ids in snapshot.signals_of.items() for signal_id in ids}
    new_of_old: dict[str, set[str]] = defaultdict(set)
    old_of_new: dict[str, set[str]] = defaultdict(set)
    current = {problem.id for problem in problems.find(conn)}
    for problem_id in current:
        for signal_id in problems.signal_ids(conn, problem_id):
            if signal_id in owners:
                new_of_old[owners[signal_id]].add(problem_id)
                old_of_new[problem_id].add(owners[signal_id])
    old_ids = set(snapshot.signals_of)
    by_sequence = sorted(old_of_new.items(), key=lambda item: parse_sequence(item[0]))
    return {
        "inherited": [{"problemId": problem_id, "previousId": problem_id}
                      for problem_id in sorted(current & old_ids, key=parse_sequence)],
        "split": [{"previousId": old, "problemIds": sorted(ids, key=parse_sequence)}
                  for old, ids in sorted(new_of_old.items(), key=lambda item: parse_sequence(item[0])) if len(ids) > 1],
        "merged": [{"problemId": new, "previousIds": sorted(ids, key=parse_sequence)}
                   for new, ids in by_sequence if len(ids) > 1],
    }


class _Events:
    """按时间顺序重新施加读出的事件；问题已不存在或转换被拒绝的写入 notes。"""

    def __init__(self, service: AggregateService, work: sqlite3.Connection, run_id: str,
                 events: list[ProblemEventRecord], notes: list[str]) -> None:
        self.service = service
        self.work = work
        self.run_id = run_id
        self.pending = deque(events)
        self.notes = notes

    def until(self, run: Run | None) -> None:
        while self.pending and (run is None or self.pending[0].at <= run.started_at):
            self._apply(self.pending.popleft())

    def _apply(self, record: ProblemEventRecord) -> None:
        problem = problems.get(self.work, record.problem_id)
        if problem is None:
            self.notes.append(f"{record.problem_id} 的 {record.event.value} 事件没有重新施加：重放后没有这个问题")
            return
        config = self.service.deps.config
        changeset = ChangeSet.start(self.work, self.run_id, record.at, config.whole_threshold("suppressionDays"))
        try:
            changeset.transition(problem, record.event, context_from_dict(record.detail.get(CONTEXT_KEY, {})),
                                 operation=record.operation, reason=record.reason)
            apply.commit(self.work, changeset, self.service.deps.clock, self.service.deps.layout, side_effects=False)
        except (TransitionRejected, MergeRejected) as error:
            self.notes.append(f"{record.problem_id} 的 {record.event.value} 事件没有重新施加：{error}")


def _items(conn: sqlite3.Connection) -> list[tuple[Run, list[Signal]]]:
    return [(run, select.pending_signals(conn, run)) for run in select.pending_runs(conn)]


def rebuild(service: AggregateService, rules: Rules) -> AggregateResult:
    deps = service.deps
    if not service.output_mode:
        path = deps.layout.database_backup(deps.clock.now())
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = sqlite3.connect(path)
        with backup:
            deps.conn.backup(backup)
        backup.close()
    work = service.work_connection()
    snapshot = Snapshot.read(work)
    record, tracer = service.begin(work)
    notes: list[str] = []
    inheritance = snapshot.inheritance()
    report: dict[str, Any] | None = None
    try:
        with transaction(work):
            problems.clear(work)
            runs.reset_aggregation(work)
            signals.reset_aggregation(work)
            replayed = _Events(service, work, record.id, snapshot.events, notes)
            processed = service.process_runs(work, record, tracer, _items(work), rules, snapshot.replayer(inheritance),
                                             inheritance=inheritance, before=replayed.until)
            if processed.failure is not None:
                raise RebuildAborted(processed.failure)
            replayed.until(None)
            report = _report(snapshot, work)
    except RebuildAborted as error:
        processed.changesets.clear()
        processed.reopened.clear()
        processed.failure = f"整体重放已回滚：{error}"
    return service.finish(work, record, tracer, processed, notes=notes, rebuild=report)
