"""新建 Issue(architecture/06 10.1 第 3b 步)：分配编号、写文件与索引、回写问题的 issue_id；以及用户需求的 Issue
(architecture/06 10.7)：不关联问题，状态直接为待修。

编号分配后不回收；写文件与索引、回写问题在一个事务中，任一步失败时索引与问题回滚，已写出的文件由下一次 sync 或
reindex 以文件为准补齐。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import tzinfo
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import IssueOrigin, IssueStatus, Severity, SizeTier, TaskType
from tightrein.domain.ids import parse_sequence
from tightrein.domain.issue import Issue
from tightrein.domain.problem import Problem
from tightrein.pipeline.issue.render import issue as template
from tightrein.pipeline.issue.steps.frontmatter import issue_for
from tightrein.pipeline.issue.steps.slug import normalize
from tightrein.store import sequences
from tightrein.store.db import transaction
from tightrein.store.files import issue_files
from tightrein.store.files.issue_files import IssueDocument
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import problems
from tightrein.store.repos.issues import IssueRecord

MANUAL_SLUG = "manual"
MANUAL_SOURCE = "用户需求"


def create(conn: sqlite3.Connection, layout: WorkspaceLayout, clock: Clock, *, run_id: str, problem: Problem,
           outputs: Mapping[str, Any], slug: str, title: str, render: Callable[[str], str],
           source: str | None = None) -> IssueRecord:
    """render 按分配到的 Issue 编号生成正文(「下一步」写本地编号)。"""
    issue_id = sequences.next_issue_id(conn)
    findings = layout.relative(layout.finding(problem.id))
    issue = issue_for(issue_id, slug, title, problem, outputs, clock.now(), findings, source)
    with transaction(conn):
        record = issue_files.write(conn, layout, IssueDocument(issue, render(issue_id), run_id))
        problems.save(conn, replace(problem, issue_id=issue_id))
    return record


def create_manual(conn: sqlite3.Connection, layout: WorkspaceLayout, clock: Clock, *, title: str, requirement: str,
                  severity: Severity, slug_max_length: int, language: str, zone: tzinfo | None = None,
                  depends_on: str | None = None, source: str = MANUAL_SOURCE, task_type: TaskType | None = None,
                  size_tier: SizeTier | None = None, parent: str | None = None,
                  repro_test: bool = True) -> IssueRecord:
    """用户需求的 Issue：正文的「问题」一节为用户原文，简称取标题中的英文词，取不到时为 manual-<序号>。拆分出的后续
    子任务同样以这种 Issue 建立，parent 为父 Issue、depends_on 为前一个子任务，source 写进头信息与第一行历史，任务类型
    与规模档写进头信息供修复分流；repro_test 为假(fix.repro.skipTypes 中的类型)时验收标准写明不写复现测试。"""
    issue_id = sequences.next_issue_id(conn)
    slug = normalize(title, slug_max_length) or f"{MANUAL_SLUG}-{parse_sequence(issue_id)}"
    now = clock.now()
    issue = Issue(id=issue_id, slug=slug, title=title, status=IssueStatus.TODO, severity=severity, created_at=now,
                  updated_at=now, origin=IssueOrigin.MANUAL, depends_on=depends_on, task_type=task_type,
                  size_tier=size_tier, parent=parent, source=source)
    history = template.history_line(now, f"创建({source}，免审阅)：严重度 {severity.value}", zone)
    text = template.manual_document(issue_id, title, requirement, history, language, repro_test=repro_test)
    with transaction(conn):
        return issue_files.write(conn, layout, IssueDocument(issue, text))
