"""triage_results 表：分诊结论。同一问题每次分诊一个 attempt；run_id、created_at、outcome_at 只存在于表中。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import (
    Complexity,
    Disposition,
    IssueLabel,
    Severity,
    SizeTier,
    TaskType,
    Treatment,
    TriageOutcome,
    Verdict,
)
from tightrein.domain.triage import IntroducedBy, RootCause, TriageFlags, TriageResult
from tightrein.store.repos.table import JSON, TIME, enum_codec, given, select, upsert

TABLE = "triage_results"


@dataclass(frozen=True)
class TriageRecord:
    result: TriageResult
    run_id: str
    created_at: datetime
    outcome_at: datetime | None = None


def introduced_by_dict(value: IntroducedBy) -> dict[str, Any]:
    return {"commit": value.commit, "author": value.author, "pr": value.pr}


def introduced_by_from(data: dict[str, Any]) -> IntroducedBy:
    return IntroducedBy(data["commit"], data.get("author"), data.get("pr"))


def to_row(record: TriageRecord) -> dict[str, Any]:
    result = record.result
    return {
        "problem_id": result.problem_id,
        "attempt": result.attempt,
        "run_id": record.run_id,
        "verdict": result.verdict.value,
        "severity": enum_codec(Severity).to_column(result.severity),
        "complexity": enum_codec(Complexity).to_column(result.complexity),
        "root_causes": JSON.encode(
            [{"file": cause.file, "line": cause.line, "symbol": cause.symbol} for cause in result.root_causes]
        ),
        "introduced_by": JSON.to_column(
            introduced_by_dict(result.introduced_by) if result.introduced_by is not None else None
        ),
        "disposition": result.disposition.value,
        "reason": result.reason,
        "triage_commit": result.triage_commit,
        "refuter_verdict": enum_codec(Verdict).to_column(result.refuter_verdict),
        "flags": JSON.encode({
            "design": result.flags.design,
            "dataStructure": result.flags.data_structure,
            "publicContract": result.flags.public_contract,
        }),
        "labels": JSON.encode([label.value for label in result.labels]),
        "outcome": enum_codec(TriageOutcome).to_column(result.outcome),
        "treatment": enum_codec(Treatment).to_column(result.treatment),
        "task_type": enum_codec(TaskType).to_column(result.task_type),
        "size_tier": enum_codec(SizeTier).to_column(result.size_tier),
        "outcome_at": TIME.to_column(record.outcome_at),
        "created_at": format_iso(record.created_at),
    }


def from_row(row: sqlite3.Row) -> TriageRecord:
    introduced_by = JSON.from_column(row["introduced_by"])
    flags = JSON.decode(row["flags"])
    result = TriageResult(
        problem_id=row["problem_id"],
        attempt=row["attempt"],
        verdict=Verdict(row["verdict"]),
        disposition=Disposition(row["disposition"]),
        reason=row["reason"],
        triage_commit=row["triage_commit"],
        severity=enum_codec(Severity).from_column(row["severity"]),
        complexity=enum_codec(Complexity).from_column(row["complexity"]),
        root_causes=tuple(
            RootCause(item["file"], item["line"], item.get("symbol")) for item in JSON.decode(row["root_causes"])
        ),
        introduced_by=introduced_by_from(introduced_by) if introduced_by is not None else None,
        refuter_verdict=enum_codec(Verdict).from_column(row["refuter_verdict"]),
        flags=TriageFlags(flags["design"], flags["dataStructure"], flags["publicContract"]),
        labels=tuple(IssueLabel(label) for label in JSON.decode(row["labels"])),
        outcome=enum_codec(TriageOutcome).from_column(row["outcome"]),
        treatment=enum_codec(Treatment).from_column(row["treatment"]),
        task_type=enum_codec(TaskType).from_column(row["task_type"]),
        size_tier=enum_codec(SizeTier).from_column(row["size_tier"]),
    )
    return TriageRecord(result, row["run_id"], TIME.decode(row["created_at"]), TIME.from_column(row["outcome_at"]))


def save(conn: sqlite3.Connection, record: TriageRecord) -> None:
    upsert(conn, TABLE, to_row(record), ("problem_id", "attempt"))


def get(conn: sqlite3.Connection, problem_id: str, attempt: int) -> TriageRecord | None:
    rows = select(conn, TABLE, {"problem_id": problem_id, "attempt": attempt})
    return from_row(rows[0]) if rows else None


def for_problem(conn: sqlite3.Connection, problem_id: str) -> list[TriageRecord]:
    return [from_row(row) for row in select(conn, TABLE, {"problem_id": problem_id}, "attempt")]


def latest(conn: sqlite3.Connection, problem_id: str) -> TriageRecord | None:
    records = for_problem(conn, problem_id)
    return records[-1] if records else None


def next_attempt(conn: sqlite3.Connection, problem_id: str) -> int:
    row = conn.execute("SELECT MAX(attempt) FROM triage_results WHERE problem_id = ?", (problem_id,)).fetchone()
    return (row[0] or 0) + 1


def find(
    conn: sqlite3.Connection, *, disposition: Disposition | None = None, run_id: str | None = None
) -> list[TriageRecord]:
    """按给出的条件等值过滤，按写入时间升序。"""
    filters = given({"disposition": enum_codec(Disposition).to_column(disposition), "run_id": run_id})
    return [from_row(row) for row in select(conn, TABLE, filters, "created_at, problem_id, attempt")]


def set_outcome(
    conn: sqlite3.Connection, problem_id: str, attempt: int, outcome: TriageOutcome, at: datetime
) -> TriageRecord:
    """回填分诊结论的实际结果；返回更新后的记录。"""
    record = get(conn, problem_id, attempt)
    if record is None:
        raise LookupError(f"没有问题 {problem_id} 第 {attempt} 次的分诊结论")
    updated = replace(record, result=replace(record.result, outcome=outcome), outcome_at=at)
    save(conn, updated)
    return updated
