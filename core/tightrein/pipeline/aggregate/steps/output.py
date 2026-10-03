"""第 6 步 输出(design 2.13，architecture/05 3.8)：运行级与问题级交接文档。

问题级交接文档给本次变为「新发现」或「回归」的问题各一份(分诊的输入)：问题、本次的转换、最近一条信号、同一问题下
不同角色或位置的信号样本、复现确认与回归时的 Issue 编号。文档内容在 apply 之后从数据库读取；
中断恢复时本次的状态变化从该运行写下的 problem_events 还原。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.contracts import versions
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import HandoffStatus, ProblemStatus, RunStage
from tightrein.domain.ids import handoff_id
from tightrein.domain.problem import role_of
from tightrein.domain.reproduce import strategy
from tightrein.domain.signal import Signal
from tightrein.pipeline.aggregate.changeset import ChangeSet, ProcessedRun, Reproduction, StatusChange
from tightrein.store.files import handoff_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import problem_events, problems, signals

ENVELOPE = "handoff/envelope.schema.json"
STAGE = RunStage.AGGREGATE
FOR_TRIAGE = frozenset({ProblemStatus.NEW, ProblemStatus.REGRESSED})
COUNTED = (ProblemStatus.NEW, ProblemStatus.REGRESSED, ProblemStatus.RESOLVED, ProblemStatus.ONGOING)
SAMPLE_LIMIT = 5  # handoff/outputs/aggregate.schema.json 中 samples 的 maxItems
TO_TRIAGE = "交给 triage"
NO_TRIAGE = "无需分诊"
RECOVERED = "上次聚合在交接文档写完前中断，本文档按其写入的问题事件补写；处理的运行无法还原"


@dataclass
class Summary:
    processed: list[ProcessedRun] = field(default_factory=list)
    changes: list[StatusChange] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    reproduction: dict[str, Reproduction] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, changesets: Sequence[ChangeSet], reopened: Sequence[str]) -> Summary:
        summary = cls(reopened=list(reopened))
        for changeset in changesets:
            summary.processed += changeset.processed
            summary.changes += changeset.changes
            summary.reproduction.update(changeset.reproduction)
            summary.notes += changeset.notes
        return summary

    @classmethod
    def from_events(cls, conn: sqlite3.Connection, run_id: str) -> Summary:
        records = problem_events.TABLE.find(conn, run_id=run_id)
        changes = [StatusChange(record.problem_id, record.from_status, record.to_status, record.event)
                   for record in records if record.to_status is not None and record.from_status is not record.to_status]
        return cls(changes=changes, notes=[RECOVERED])


def _triaged(conn: sqlite3.Connection, summary: Summary) -> list[tuple[str, StatusChange]]:
    """本次变为新发现或回归、现在仍是这个状态且没有被合并的问题，与对应的那次转换。"""
    latest: dict[str, StatusChange] = {}
    for change in summary.changes:
        if change.to_status in FOR_TRIAGE:
            latest[change.problem_id] = change
    result = []
    for problem_id, change in latest.items():
        problem = problems.get(conn, problem_id)
        if problem is not None and problem.status is change.to_status and problem.merged_into is None:
            result.append((problem_id, change))
    return result


def _samples(found: Sequence[Signal], latest: Signal) -> list[dict[str, Any]]:
    seen = {(role_of(latest), latest.location)}
    samples = []
    for signal in reversed(found):
        key = (role_of(signal), signal.location)
        if key in seen:
            continue
        seen.add(key)
        samples.append(signal.to_dict())
        if len(samples) == SAMPLE_LIMIT:
            break
    return samples


def _envelope(run_id: str, subject: dict[str, str], outputs: Mapping[str, Any], next_action: str, clock: Clock,
              status: HandoffStatus = HandoffStatus.OK, reason: str | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schemaVersion": versions.current(ENVELOPE), "runId": run_id, "stage": STAGE.value, "subject": subject,
        "status": status.value, "inputsRef": {}, "outputs": dict(outputs), "nextAction": next_action,
        "createdAt": format_iso(clock.now()),
    }
    if reason is not None:
        document["blockedReason"] = reason
    return document


def problem_document(conn: sqlite3.Connection, run_id: str, problem_id: str, change: StatusChange,
                     summary: Summary, clock: Clock) -> dict[str, Any]:
    problem = problems.get(conn, problem_id)
    found = signals.get_many(conn, problems.signal_ids(conn, problem_id))
    if problem is None or not found:
        raise LookupError(f"问题 {problem_id} 不存在或没有信号")
    latest = found[-1]
    reproduction = summary.reproduction.get(problem_id, Reproduction(strategy(problem.probe, latest.check)))
    outputs = {
        "problem": problem.to_dict(),
        "transition": change.to_dict(),
        "latestSignal": latest.to_dict(),
        "samples": _samples(found, latest),
        "reproduction": reproduction.to_dict(),
        "issueId": problem.issue_id if problem.status is ProblemStatus.REGRESSED else None,
    }
    return _envelope(run_id, {"type": "problem", "id": problem_id}, outputs, TO_TRIAGE, clock)


def run_outputs(conn: sqlite3.Connection, summary: Summary, triaged: Sequence[str],
                rebuild: Mapping[str, Any] | None = None) -> dict[str, Any]:
    counts = {status.value: sum(1 for change in summary.changes if change.to_status is status) for status in COUNTED}
    counts["pending"] = len(problems.find(conn, statuses=[ProblemStatus.PENDING]))
    outputs: dict[str, Any] = {
        "processedRuns": [item.to_dict() for item in summary.processed],
        "counts": counts,
        "forTriage": [{"problemId": problem_id, "handoff": f"handoff/{handoff_id(STAGE.value, problem_id)}.json"}
                      for problem_id in triaged],
        "statusChanges": [change.to_dict() for change in summary.changes],
        "reopenedIssues": list(summary.reopened),
        "notes": list(summary.notes),
    }
    if rebuild is not None:
        outputs["rebuild"] = dict(rebuild)
    return outputs


def documents(conn: sqlite3.Connection, run_id: str, summary: Summary, clock: Clock, *,
              failure: str | None = None, rebuild: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """运行级文档在前，问题级文档按问题编号在后。"""
    triaged = _triaged(conn, summary)
    ids = [problem_id for problem_id, _ in triaged]
    outputs = run_outputs(conn, summary, ids, rebuild)
    status = HandoffStatus.FAILED if failure is not None else HandoffStatus.OK
    action = f"查看失败原因后重新运行 tightrein aggregate：{failure}" if failure else (TO_TRIAGE if ids else NO_TRIAGE)
    run_document = _envelope(run_id, {"type": "run", "id": run_id}, outputs, action, clock, status, failure)
    return [run_document, *(problem_document(conn, run_id, problem_id, change, summary, clock)
                            for problem_id, change in triaged)]


def write(layout: WorkspaceLayout, conn: sqlite3.Connection | None, clock: Clock,
          documents_: Sequence[dict[str, Any]]) -> list[Path]:
    """conn 为空(--output 模式)时不写 handoffs 表。"""
    return [handoff_files.write(layout, document, clock, conn=conn).path for document in documents_]
