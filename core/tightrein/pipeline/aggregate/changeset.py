"""ChangeSet(architecture/05 3.4)：处理一个 collect 运行(或一次人工操作)要写入的全部变化。

各步只读数据库、只修改 ChangeSet；apply 在一个事务中把它写入。查找问题时先看本 ChangeSet 中已修改或新建的，再看数据库。
新问题的编号按 sequences 的当前值在内存中顺延，apply 时推进序列；全局锁保证同一时间只有一个写入者。

状态转换经 transition 施加：调用 domain 的问题状态机，按返回的副作用修改问题或登记待执行的动作(抑制规则、
Issue 重新打开、合并)，并写一条 problem_events；分诊与 Issue 模块写入的转换也经这里(Issue 以重复关闭时问题改挂到
被重复的 Issue)。事件的 detail.context 记录转换所用的 ProblemContext，
整体重放按它重新施加人工操作与分诊、Issue 关闭等其他模块写入的事件。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from tightrein.domain import ids
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import (
    CloseReason,
    Disposition,
    Probe,
    ProblemEffect,
    ProblemEvent,
    ProblemStatus,
    ReproduceStrategy,
)
from tightrein.domain.problem import IgnoreCondition, Problem, ProblemContext
from tightrein.domain.problem import transition as problem_transition
from tightrein.domain.run import Run
from tightrein.domain.signal import Signal
from tightrein.domain.state_machine import InvalidTransition
from tightrein.domain.suppression import SuppressionRule
from tightrein.store import sequences
from tightrein.store.repos import problems
from tightrein.store.repos.problem_events import OPERATION_AUTO, ProblemEventRecord

CONTEXT_KEY = "context"


class TransitionRejected(Exception):
    """问题状态机拒绝了转换；带上问题编号，整个 collect 运行的处理中止。"""

    def __init__(self, problem_id: str, error: InvalidTransition) -> None:
        self.problem_id = problem_id
        super().__init__(f"问题 {problem_id}：{error}")


@dataclass(frozen=True)
class StatusChange:
    problem_id: str
    from_status: ProblemStatus | None
    to_status: ProblemStatus
    event: ProblemEvent

    def to_dict(self) -> dict[str, Any]:
        return {"problemId": self.problem_id, "from": self.from_status.value if self.from_status else None,
                "to": self.to_status.value, "event": self.event.value}


@dataclass
class ProcessedRun:
    run_id: str
    probe: Probe
    grouped: int = 0
    suppressed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"runId": self.run_id, "probe": self.probe.value, "grouped": self.grouped,
                "suppressed": self.suppressed}


@dataclass(frozen=True)
class Reproduction:
    strategy: ReproduceStrategy
    attempts: int = 0
    reproduced: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"strategy": self.strategy.value, "attempts": self.attempts, "reproduced": self.reproduced}


@dataclass(frozen=True)
class IssueReopen:
    issue_id: str
    problem_id: str
    release: str | None
    signal_ids: tuple[str, ...]


def context_to_dict(context: ProblemContext) -> dict[str, Any]:
    """只写与缺省值不同的字段。"""
    data: dict[str, Any] = {}
    if context.disposition is not None:
        data["disposition"] = context.disposition.value
    if context.close_reason is not None:
        data["closeReason"] = context.close_reason.value
    if context.ready_to_resolve:
        data["readyToResolve"] = True
    if context.regressed:
        data["regressed"] = True
    if context.issue_id is not None:
        data["issueId"] = context.issue_id
    if context.ignore_until is not None:
        data["ignoreUntil"] = context.ignore_until.to_dict()
    for key, value in (("mergeTarget", context.merge_target), ("duplicateOf", context.duplicate_of),
                       ("release", context.release)):
        if value is not None:
            data[key] = value
    return data


def context_from_dict(data: dict[str, Any]) -> ProblemContext:
    ignore_until = data.get("ignoreUntil")
    return ProblemContext(
        disposition=Disposition(data["disposition"]) if "disposition" in data else None,
        close_reason=CloseReason(data["closeReason"]) if "closeReason" in data else None,
        ready_to_resolve=bool(data.get("readyToResolve", False)),
        regressed=bool(data.get("regressed", False)),
        issue_id=data.get("issueId"),
        ignore_until=IgnoreCondition.from_dict(ignore_until) if ignore_until is not None else None,
        merge_target=data.get("mergeTarget"),
        duplicate_of=data.get("duplicateOf"),
        release=data.get("release"),
    )


@dataclass
class Inheritance:
    """整体重放时的编号继承：新问题继承其第一条信号所属旧问题的编号(该编号尚未被继承时)，继承时带上旧问题的
    Issue 编号。一个 Inheritance 在整次重放的全部 ChangeSet 之间共用。"""

    owners: dict[str, str]
    issue_ids: dict[str, str]
    claimed: set[str] = field(default_factory=set)

    def _claim(self, old: str | None) -> tuple[str | None, bool]:
        if old is None:
            return None, False
        if old in self.claimed:
            return None, True
        self.claimed.add(old)
        return old, False

    def for_signal(self, signal_id: str) -> tuple[str | None, bool]:
        """(继承的编号, 是否为拆出的问题)。"""
        return self._claim(self.owners.get(signal_id))



@dataclass
class ChangeSet:
    run_id: str | None
    now: datetime
    next_number: int
    suppression_days: int
    runs: dict[str, Run] = field(default_factory=dict)
    signals: dict[str, Signal] = field(default_factory=dict)
    problems: dict[str, Problem] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    problem_signals: list[tuple[str, str]] = field(default_factory=list)
    merges: list[tuple[str, str]] = field(default_factory=list)
    events: list[ProblemEventRecord] = field(default_factory=list)
    changes: list[StatusChange] = field(default_factory=list)
    suppressions: list[SuppressionRule] = field(default_factory=list)
    reopened: list[IssueReopen] = field(default_factory=list)
    reproduction: dict[str, Reproduction] = field(default_factory=dict)
    processed: list[ProcessedRun] = field(default_factory=list)
    occurred: dict[str, list[str]] = field(default_factory=dict)
    regression_hits: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    inheritance: Inheritance | None = None

    @classmethod
    def start(cls, conn: sqlite3.Connection, run_id: str | None, now: datetime, suppression_days: int) -> ChangeSet:
        return cls(run_id, now, sequences.current(conn, sequences.PROBLEM) + 1, suppression_days)

    def allocate(self, preferred: str | None = None) -> str:
        """新问题的编号；preferred 给出时(整体重放继承旧编号)直接使用，序列推进到不小于它。"""
        if preferred is not None:
            self.next_number = max(self.next_number, ids.parse_sequence(preferred) + 1)
            return preferred
        number = self.next_number
        self.next_number += 1
        return ids.problem_id(number)

    def problem(self, conn: sqlite3.Connection, problem_id: str) -> Problem | None:
        return self.problems.get(problem_id) or problems.get(conn, problem_id)

    def by_fingerprint(self, conn: sqlite3.Connection, fingerprint: str) -> Problem | None:
        for problem in self.problems.values():
            if problem.fingerprint == fingerprint and problem.merged_into is None:
                return problem
        found = problems.by_fingerprint(conn, fingerprint)
        return self.problems.get(found.id, found) if found is not None else None

    @property
    def current(self) -> ProcessedRun:
        """正在处理的 collect 运行的计数，由 register 登记。"""
        if not self.processed:
            raise ValueError("还没有登记正在处理的运行")
        return self.processed[-1]

    def register(self, run: Run) -> None:
        """登记正在处理的 collect 运行：计数从这里开始，运行记为已聚合。"""
        if run.probe is None:
            raise ValueError(f"{run.id} 不是 collect 运行")
        self.processed.append(ProcessedRun(run.id, run.probe))
        self.runs[run.id] = replace(run, aggregated_at=self.now)

    def put_signal(self, signal: Signal) -> None:
        self.signals[signal.id] = signal

    def put_problem(self, problem: Problem, *, created: bool = False) -> None:
        self.problems[problem.id] = problem
        if created:
            self.created.append(problem.id)

    def record_occurrence(self, problem_id: str, signal_id: str) -> None:
        self.problem_signals.append((problem_id, signal_id))
        self.occurred.setdefault(problem_id, []).append(signal_id)

    def transition(self, problem: Problem, event: ProblemEvent, context: ProblemContext = ProblemContext(), *,
                   operation: str = OPERATION_AUTO, reason: str | None = None,
                   suppression_expires: date | None = None) -> Problem:
        try:
            target, effects = problem_transition(problem.status, event, context)
        except InvalidTransition as error:
            raise TransitionRejected(problem.id, error) from error
        updated = replace(problem, status=target)
        for effect in effects:
            updated = self._apply_effect(updated, effect.kind, context, reason, suppression_expires)
        self.problems[updated.id] = updated
        self.events.append(ProblemEventRecord(
            problem_id=problem.id, at=self.now, event=event, operation=operation, from_status=problem.status,
            to_status=target, run_id=self.run_id, reason=reason, detail={CONTEXT_KEY: context_to_dict(context)},
        ))
        if target is not problem.status:
            self.changes.append(StatusChange(problem.id, problem.status, target, event))
        return updated

    def _apply_effect(self, problem: Problem, kind: Any, context: ProblemContext, reason: str | None,
                      expires: date | None) -> Problem:
        if kind is ProblemEffect.SET_IGNORE_UNTIL:
            return replace(problem, ignore_until=context.ignore_until)
        if kind is ProblemEffect.CLEAR_IGNORE_UNTIL:
            return replace(problem, ignore_until=None)
        if kind is ProblemEffect.RESET_CLEAN_RUNS:
            return replace(problem, clean_covered_runs=0)
        if kind is ProblemEffect.MARK_INTERMITTENT:
            return replace(problem, intermittent=True)
        if kind is ProblemEffect.RECORD_RESOLVED_RELEASE:
            return replace(problem, resolved_release=context.release)
        if kind is ProblemEffect.CREATE_SUPPRESSION:
            today = self.now.date()
            rule = SuppressionRule.for_fingerprint(problem.fingerprint, reason or "判为误报", today,
                                                   self.suppression_days)
            if expires is not None:
                rule = replace(rule, expires_on=expires)
            self.suppressions.append(rule)
            return problem
        if kind is ProblemEffect.ISSUE_REGRESSED:
            if context.issue_id is None:
                raise ValueError(f"问题 {problem.id} 回归，但没有给出 Issue 编号")
            signal_ids = tuple(self.occurred.get(problem.id, ()))
            self.reopened.append(IssueReopen(context.issue_id, problem.id, problem.last_seen_release, signal_ids))
            return problem
        if kind is ProblemEffect.MERGE_INTO:
            if context.merge_target is None:
                raise ValueError(f"问题 {problem.id} 合并时没有给出目标")
            self.merges.append((context.merge_target, problem.id))
            return problem
        if kind is ProblemEffect.MOVE_TO_DUPLICATE_ISSUE:
            return replace(problem, issue_id=context.duplicate_of)
        raise ValueError(f"聚合不处理副作用 {kind.value}")

    def to_dict(self) -> dict[str, Any]:
        """--output 模式写入 changeset.json 的内容。"""
        return {
            "runId": self.run_id,
            "processedRuns": [item.to_dict() for item in self.processed],
            "signals": [signal.to_dict() for signal in self.signals.values()],
            "problems": [problem.to_dict() for problem in self.problems.values()],
            "created": list(self.created),
            "problemSignals": [{"problemId": pid, "signalId": sid} for pid, sid in self.problem_signals],
            "merges": [{"target": target, "source": source} for target, source in self.merges],
            "events": [{"problemId": event.problem_id, "at": format_iso(event.at), "event": event.event.value,
                        "operation": event.operation,
                        "from": event.from_status.value if event.from_status else None,
                        "to": event.to_status.value if event.to_status else None, "reason": event.reason,
                        "detail": event.detail} for event in self.events],
            "statusChanges": [change.to_dict() for change in self.changes],
            "suppressions": [rule.to_dict() for rule in self.suppressions],
            "reopenedIssues": [{"issueId": item.issue_id, "problemId": item.problem_id, "release": item.release,
                                "signalIds": list(item.signal_ids)} for item in self.reopened],
            "reproduction": {pid: item.to_dict() for pid, item in self.reproduction.items()},
            "notes": list(self.notes),
        }
