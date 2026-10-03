"""fix、verify、release 与 learn 共用：一次命令调用的运行记录、追踪与交接文档的外层(architecture/07 1.1，design 10.2)。

每次命令调用(例如 fix plan、verify local、release push)是一次运行：写 runs 表与 run span，交接文档按
`<环节>-<Issue 编号>`(验证为 `verify-<阶段>-<Issue 编号>`)写入本次运行的目录。--output 模式不写数据库。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tightrein.contracts import versions
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import HandoffStatus, RunStage, RunStatus, VerifyPhase
from tightrein.domain.run import Run
from tightrein.observability.events import EventLog
from tightrein.observability.tracing import Tracer
from tightrein.store.files import handoff_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, runs

ENVELOPE = "handoff/envelope.schema.json"
SUBJECT = "issue"
RUN_STATUS = {HandoffStatus.OK: RunStatus.OK, HandoffStatus.BLOCKED: RunStatus.BLOCKED,
              HandoffStatus.FAILED: RunStatus.FAILED}


@dataclass
class StageRun:
    run: Run
    tracer: Tracer
    layout: WorkspaceLayout
    conn: sqlite3.Connection
    clock: Clock

    @property
    def id(self) -> str:
        return self.run.id

    @property
    def output_mode(self) -> bool:
        return self.layout.output_dir is not None

    def handoff(self, stage: RunStage, issue_id: str, status: HandoffStatus, outputs: Mapping[str, Any],
                next_action: str, reason: str | None = None, phase: VerifyPhase | None = None,
                subject_type: str = SUBJECT) -> Path:
        """subject_type 为对象类型：修复、验证与发布为 issue，learn 为 week 或 run。"""
        document: dict[str, Any] = {
            "schemaVersion": versions.current(ENVELOPE), "runId": self.id, "stage": stage.value,
            "subject": {"type": subject_type, "id": issue_id}, "status": status.value, "inputsRef": {},
            "outputs": dict(outputs), "nextAction": next_action, "createdAt": format_iso(self.clock.now()),
        }
        if reason is not None:
            document["blockedReason"] = reason
        written = handoff_files.write(self.layout, document, self.clock, conn=None if self.output_mode else self.conn,
                                      phase=phase)
        return written.path

    def end(self, status: HandoffStatus) -> Run:
        self.run = replace(self.run, status=RUN_STATUS[status], ended_at=self.clock.now())
        if not self.output_mode:
            runs.save(self.conn, self.run)
        return self.run


def begin(stage: RunStage, layout: WorkspaceLayout, conn: sqlite3.Connection, clock: Clock, events: EventLog,
          commit: str | None = None) -> StageRun:
    started = clock.now()
    run_id = runs.free_id(conn, started, stage)
    tracer = Tracer(events, clock, run_id=run_id, stage=stage.value)
    run = Run(run_id, stage, started, RunStatus.RUNNING, target_commit=commit, trace_id=tracer.trace_id)
    if layout.output_dir is None:
        runs.save(conn, run)
    return StageRun(run, tracer, layout, conn, clock)


def latest_outputs(conn: sqlite3.Connection, layout: WorkspaceLayout, stage: RunStage, issue_id: str,
                   phase: VerifyPhase | None = None) -> tuple[str, dict[str, Any]] | None:
    """该环节该 Issue 最近一份交接文档的状态与 outputs。"""
    record = handoffs.get(conn, stage, issue_id, phase=phase)
    if record is None:
        return None
    document = handoff_files.read(layout.root / record.path)
    return document["status"], document["outputs"]
