"""分诊结论的实际结果回填(design 3.10)：判为误报的问题在之后的运行中不再出现，即为判对。

「之后的运行」取分诊之后同探针、覆盖该问题范围的可信运行(domain.problem.is_covered)，达到
thresholds.learn.falsePositiveCleanRuns 次，且其间没有指纹(含别名)相同的信号时回填 correct；
被抑制的信号不关联到问题，但仍保留指纹，因此按指纹查。其余实际结果由 issue 与 triage 写入。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import Disposition, RunStage, RunStatus, TriageOutcome
from tightrein.domain.problem import is_covered
from tightrein.store.repos import problems, runs, triage


def _seen_since(conn: sqlite3.Connection, fingerprints: list[str], since: datetime) -> bool:
    marks = ", ".join("?" for _ in fingerprints)
    row = conn.execute(f"SELECT 1 FROM signals WHERE fingerprint IN ({marks}) AND occurred_at > ? LIMIT 1",
                       (*fingerprints, format_iso(since))).fetchone()
    return row is not None


def backfill(conn: sqlite3.Connection, config: ProjectConfig, now: datetime) -> list[tuple[str, int]]:
    """返回回填的(问题编号、分诊次数)。"""
    required = config.whole_threshold("learn.falsePositiveCleanRuns")
    filled = []
    for record in triage.find(conn, disposition=Disposition.FALSE_POSITIVE):
        result = record.result
        latest = triage.latest(conn, result.problem_id)
        problem = problems.get(conn, result.problem_id)
        if result.outcome is not None or latest is None or latest.result.attempt != result.attempt or problem is None:
            continue
        covered = [run for run in runs.find(conn, stage=RunStage.COLLECT, probe=problem.probe, status=RunStatus.OK)
                   if run.started_at > record.created_at and is_covered(problem, run)]
        fingerprints = [problem.fingerprint, *problems.aliases(conn, problem.id)]
        if len(covered) >= required and not _seen_since(conn, fingerprints, record.created_at):
            triage.set_outcome(conn, result.problem_id, result.attempt, TriageOutcome.CORRECT, now)
            filled.append((result.problem_id, result.attempt))
    return filled
