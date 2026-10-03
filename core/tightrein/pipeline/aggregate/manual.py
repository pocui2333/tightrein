"""人工操作问题(design 2.9，architecture/05 3.5)：ignore、false-positive、merge、reopen。

每个操作获取全局锁，以 ChangeSet 表达变化(事件的 operation 为 user_action)，由 apply 在一个事务中写入；
误报生成的抑制规则写入 suppressions.yaml，到期日期缺省为当天加 thresholds.suppressionDays。
问题不存在时抛出 LookupError；合并的前提不满足(B 已有 Issue、已被合并等)时抛出 MergeRejected。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import date, datetime

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import ProblemEvent, RunStage
from tightrein.domain.problem import Problem, ProblemContext, ignore_condition
from tightrein.observability.events import EventLog
from tightrein.observability.tracing import Tracer
from tightrein.pipeline.aggregate.changeset import ChangeSet
from tightrein.pipeline.aggregate.service import global_lock
from tightrein.pipeline.aggregate.steps import apply
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import problems
from tightrein.store.repos.problem_events import OPERATION_USER


class ProblemCommands:
    def __init__(self, layout: WorkspaceLayout, config: ProjectConfig, conn: sqlite3.Connection, clock: Clock,
                 events: EventLog, sleep: Callable[[float], None] = time.sleep) -> None:
        self.layout = layout
        self.config = config
        self.conn = conn
        self.clock = clock
        self.events = events
        self.sleep = sleep

    def _problem(self, problem_id: str) -> Problem:
        problem = problems.get(self.conn, problem_id)
        if problem is None:
            raise LookupError(f"没有问题 {problem_id}")
        return problem

    def _apply(self, problem_id: str, event: ProblemEvent, build: Callable[[Problem], ProblemContext], *,
               reason: str | None = None, expires: date | None = None) -> Problem:
        with global_lock(self.layout, self.config, self.clock, sleep=self.sleep):
            problem = self._problem(problem_id)
            changeset = ChangeSet.start(self.conn, None, self.clock.now(),
                                        self.config.whole_threshold("suppressionDays"))
            changeset.transition(problem, event, build(problem), operation=OPERATION_USER, reason=reason,
                                 suppression_expires=expires)
            apply.commit(self.conn, changeset, self.clock, self.layout, side_effects=True)
        Tracer(self.events, self.clock, run_id=None, stage=RunStage.AGGREGATE.value).event(
            "user_action", decision=event.value, reason=reason, attributes={"problemId": problem_id})
        return self._problem(problem_id)

    def ignore(self, problem_id: str, reason: str, *, until: datetime | None = None, occurrences: int | None = None,
               new_release: bool = False) -> Problem:
        """恢复条件任一满足即恢复；都不给即永久忽略。"""
        return self._apply(problem_id, ProblemEvent.USER_IGNORED, lambda problem: ProblemContext(
            ignore_until=ignore_condition(problem, until=until, occurrences=occurrences, new_release=new_release)),
            reason=reason)

    def false_positive(self, problem_id: str, reason: str, *, expires: date | None = None) -> Problem:
        return self._apply(problem_id, ProblemEvent.USER_FALSE_POSITIVE, lambda problem: ProblemContext(),
                           reason=reason, expires=expires)

    def merge(self, target_id: str, source_id: str) -> Problem:
        """把 source 并入 target，返回 target。"""
        self._problem(target_id)
        self._apply(source_id, ProblemEvent.MERGED, lambda problem: ProblemContext(merge_target=target_id),
                    reason=f"并入 {target_id}")
        return self._problem(target_id)

    def reopen(self, problem_id: str, reason: str | None = None) -> Problem:
        return self._apply(problem_id, ProblemEvent.USER_REOPENED, lambda problem: ProblemContext(), reason=reason)
