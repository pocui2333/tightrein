"""把问题追加到同一根因的已有 Issue(architecture/06 10.1 第 3a 步)。

问题编号加入 problems，「关联」一节补充问题，「历史」一节追加一行；新问题的严重度更高时提升 Issue 的 severity
并在历史中写明；回写问题的 issue_id。文件与索引、问题在一个事务中更新。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import tzinfo
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import Severity
from tightrein.domain.problem import Problem
from tightrein.pipeline.issue.render import issue as template
from tightrein.store.db import transaction
from tightrein.store.files import issue_files
from tightrein.store.files.issue_files import IssueDocument
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import problems
from tightrein.store.repos.issues import IssueRecord

ORDER = list(Severity)


def higher(first: Severity, second: Severity) -> Severity:
    return first if ORDER.index(first) <= ORDER.index(second) else second


def appended(document: IssueDocument, problem: Problem, outputs: Mapping[str, Any], run_id: str, clock: Clock,
             zone: tzinfo | None = None) -> IssueDocument:
    """追加后的 Issue 文件内容(纯函数，--output 模式也用它)。"""
    issue = document.issue
    severity = Severity(outputs["severity"]) if outputs.get("severity") else issue.severity
    raised = higher(severity, issue.severity)
    note = f"追加问题 {problem.id}(运行 {run_id})"
    if raised is not issue.severity:
        note += f"，严重度由 {issue.severity.value} 提升为 {raised.value}"
    body = template.append_related(document.body, problem.id, problem.title)
    body = template.append_history(body, clock.now(), note, zone)
    updated = replace(issue, problems=(*issue.problems, problem.id), severity=raised, updated_at=clock.now())
    return IssueDocument(updated, body, document.run_id)


def append(conn: sqlite3.Connection, layout: WorkspaceLayout, clock: Clock, record: IssueRecord, problem: Problem,
           outputs: Mapping[str, Any], run_id: str, zone: tzinfo | None = None) -> IssueRecord:
    current = issue_files.read(layout.root / record.path)
    document = appended(current, problem, outputs, run_id, clock, zone)
    with transaction(conn):
        written = issue_files.write(conn, layout, document)
        problems.save(conn, replace(problem, issue_id=record.issue.id))
    return written
