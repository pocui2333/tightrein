"""由执行器任务与结果登记一行环节效益(design 14.2)。各模块在调用执行器之后、写入模式下调用。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from tightrein.domain.enums import YieldOutcome
from tightrein.runner.result import RunnerResult
from tightrein.runner.task import RunnerTask
from tightrein.store.repos import stage_yield
from tightrein.store.repos.stage_yield import StageYieldRecord


def record(conn: sqlite3.Connection, task: RunnerTask, result: RunnerResult, at: datetime, *,
           outcome: YieldOutcome = YieldOutcome.PENDING, reason: str | None = None) -> int | None:
    """没有实际调用工具(当天预算用尽等)的结果不登记，返回 None。"""
    if result.attempts == 0:
        return None
    usage = result.usage
    decided = None if outcome is YieldOutcome.PENDING else at
    return stage_yield.append(conn, StageYieldRecord(
        run_id=task.run_id, stage=task.stage, role=task.role, subject_id=task.subject_id, attempt=task.attempt,
        runner_status=result.status, created_at=at, input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens, cost_usd=usage.cost_usd, outcome=outcome, outcome_reason=reason,
        decided_at=decided, tool=result.tool, model=result.model))
