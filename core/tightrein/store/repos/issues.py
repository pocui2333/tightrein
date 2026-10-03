"""issues 表：Issue 索引。Issue 文件是数据来源，本表由 files/issue_files.py 写入并可从文件重建；hold 写入前按
common.schema.json 中的定义校验。path 为相对工作区的路径，file_sha256 为写入或索引时文件内容的哈希。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from tightrein.contracts import validate as contracts
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import (
    CloseReason,
    IssueOrigin,
    IssuePhase,
    IssueStatus,
    Severity,
    SizeTier,
    TaskType,
    Treatment,
)
from tightrein.domain.issue import GithubLink, Hold, Issue
from tightrein.domain.ids import parse_sequence
from tightrein.store.repos.table import JSON, STRINGS, TIME, enum_codec, given, select, upsert
from tightrein.store.repos.triage import introduced_by_dict, introduced_by_from

TABLE = "issues"


@dataclass(frozen=True)
class IssueRecord:
    issue: Issue
    path: str
    file_sha256: str


def to_row(record: IssueRecord) -> dict[str, Any]:
    issue = record.issue
    hold = issue.hold.to_dict() if issue.hold is not None else None
    if hold is not None:
        contracts.check("common.schema.json", hold, definition="hold")
    return {
        "id": issue.id,
        "slug": issue.slug,
        "path": record.path,
        "title": issue.title,
        "status": issue.status.value,
        "close_reason": enum_codec(CloseReason).to_column(issue.close_reason),
        "severity": issue.severity.value,
        "origin": issue.origin.value,
        "treatment": enum_codec(Treatment).to_column(issue.treatment),
        "task_type": enum_codec(TaskType).to_column(issue.task_type),
        "size_tier": enum_codec(SizeTier).to_column(issue.size_tier),
        "problems": STRINGS.encode(issue.problems),
        "root_cause": STRINGS.encode(issue.root_cause),
        "introduced_by": JSON.to_column(
            introduced_by_dict(issue.introduced_by) if issue.introduced_by is not None else None
        ),
        "triage_commit": issue.triage_commit,
        "findings": issue.findings,
        "branch": issue.branch,
        "pr": issue.pr,
        "hold": JSON.to_column(hold),
        "github_number": issue.github.number if issue.github is not None else None,
        "github_url": issue.github.url if issue.github is not None else None,
        "depends_on": issue.depends_on,
        "parent": issue.parent,
        "phase": enum_codec(IssuePhase).to_column(issue.phase),
        "source": issue.source,
        "file_sha256": record.file_sha256,
        "created_at": format_iso(issue.created_at),
        "updated_at": format_iso(issue.updated_at),
    }


def from_row(row: sqlite3.Row) -> IssueRecord:
    introduced_by = JSON.from_column(row["introduced_by"])
    hold = JSON.from_column(row["hold"])
    issue = Issue(
        id=row["id"],
        slug=row["slug"],
        title=row["title"],
        status=IssueStatus(row["status"]),
        severity=Severity(row["severity"]),
        created_at=TIME.decode(row["created_at"]),
        updated_at=TIME.decode(row["updated_at"]),
        treatment=enum_codec(Treatment).from_column(row["treatment"]),
        task_type=enum_codec(TaskType).from_column(row["task_type"]),
        size_tier=enum_codec(SizeTier).from_column(row["size_tier"]),
        problems=STRINGS.decode(row["problems"]),
        root_cause=STRINGS.decode(row["root_cause"]),
        introduced_by=introduced_by_from(introduced_by) if introduced_by is not None else None,
        triage_commit=row["triage_commit"],
        findings=row["findings"],
        branch=row["branch"],
        pr=row["pr"],
        close_reason=enum_codec(CloseReason).from_column(row["close_reason"]),
        hold=Hold.from_dict(hold) if hold is not None else None,
        origin=IssueOrigin(row["origin"]),
        github=GithubLink(row["github_number"], row["github_url"]) if row["github_number"] is not None else None,
        depends_on=row["depends_on"],
        parent=row["parent"],
        phase=enum_codec(IssuePhase).from_column(row["phase"]),
        source=row["source"],
    )
    return IssueRecord(issue, row["path"], row["file_sha256"])


def save(conn: sqlite3.Connection, record: IssueRecord) -> None:
    upsert(conn, TABLE, to_row(record), ("id",))


def get(conn: sqlite3.Connection, issue_id: str) -> IssueRecord | None:
    rows = select(conn, TABLE, {"id": issue_id})
    return from_row(rows[0]) if rows else None


def find(conn: sqlite3.Connection, *, status: IssueStatus | None = None) -> list[IssueRecord]:
    """按状态过滤，不给状态时返回全部；按编号升序。"""
    rows = select(conn, TABLE, given({"status": enum_codec(IssueStatus).to_column(status)}))
    return sorted((from_row(row) for row in rows), key=lambda record: parse_sequence(record.issue.id))


def by_problem(conn: sqlite3.Connection, problem_id: str) -> list[IssueRecord]:
    """problems 列中含有该问题的 Issue。"""
    rows = conn.execute(
        "SELECT issues.* FROM issues WHERE EXISTS "
        "(SELECT 1 FROM json_each(issues.problems) WHERE json_each.value = ?)",
        (problem_id,),
    ).fetchall()
    return sorted((from_row(row) for row in rows), key=lambda record: parse_sequence(record.issue.id))


def remove(conn: sqlite3.Connection, issue_id: str) -> None:
    """删除索引行；只用于文件已不存在时的重建。"""
    conn.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
