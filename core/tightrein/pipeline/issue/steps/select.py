"""选取去向为提 Issue 且还没有 Issue 的问题，读取它们的分诊交接文档(architecture/06 10.1 第 1 步)。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from tightrein.domain.enums import Disposition, RunStage
from tightrein.domain.problem import Problem
from tightrein.store.files import handoff_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, problems, triage

SUBJECT = "problem"


def eligible(conn: sqlite3.Connection, problem: Problem) -> str | None:
    """不能提 Issue 的原因；可以时为空。"""
    if problem.merged_into is not None:
        return f"{problem.id} 已并入 {problem.merged_into}"
    if problem.issue_id is not None:
        return f"{problem.id} 已有 Issue {problem.issue_id}"
    record = triage.latest(conn, problem.id)
    if record is None or record.result.disposition is not Disposition.CREATE_ISSUE:
        return f"{problem.id} 最近一次分诊的去向不是提 Issue"
    return None


def pending(conn: sqlite3.Connection) -> list[Problem]:
    return [problem for problem in problems.find(conn) if eligible(conn, problem) is None]


def triage_outputs(layout: WorkspaceLayout, conn: sqlite3.Connection, problem_id: str) -> dict[str, Any]:
    record = handoffs.get(conn, RunStage.TRIAGE, problem_id)
    if record is None:
        raise LookupError(f"{problem_id} 没有分诊交接文档")
    return handoff_files.read(layout.root / record.path)["outputs"]


def from_input(path: Path) -> tuple[str, dict[str, Any]]:
    document = handoff_files.read(path)
    if document["stage"] != RunStage.TRIAGE.value or document["subject"]["type"] != SUBJECT:
        raise ValueError(f"{path} 不是问题的分诊交接文档")
    return document["subject"]["id"], document["outputs"]
