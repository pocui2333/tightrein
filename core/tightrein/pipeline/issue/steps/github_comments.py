"""GitHub Issue 镜像的关键节点评论入队(architecture/06 10.8)。

关键节点：放行、修复计划确认、PR 创建、PR 合并、关闭(带关闭原因)。评论在本地转换的同一事务中写进
github_mirror_comments，由下一次对齐发出；issues.tracker 不是 github 时什么都不做。完成或取消且从未建镜像的 Issue
不入队(它不会再建镜像)。评论的固定部分按 project.language(render/labels.py)，附带的备注原样。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import CloseReason, IssueEvent, IssueStatus
from tightrein.domain.issue import CLOSED, Issue
from tightrein.pipeline.issue.render import labels
from tightrein.store.repos import github_mirror

GITHUB = "github"
KEY_EVENTS = frozenset({IssueEvent.PR_CREATED, IssueEvent.PR_MERGED})


def enabled(config: ProjectConfig) -> bool:
    return config.issue_tracker == GITHUB


def approved_manual(config: ProjectConfig) -> str:
    return labels.text("approvedManual", config.language)


def plan_confirmed(config: ProjectConfig) -> str:
    return labels.text("planConfirmed", config.language)


def transition_comment(event: IssueEvent, status: IssueStatus, close_reason: CloseReason | None,
                       note: str | None, language: str) -> str | None:
    """一次转换要发的评论；不是关键节点时为 None。"""
    if event in KEY_EVENTS:
        head = labels.event(event, language)
    elif status in CLOSED and close_reason is not None:
        head = labels.text("closed", language, reason=labels.close_reason(close_reason, language))
    else:
        return None
    separator = "。" if language.startswith(("zh", "ja")) else ". "
    return head + (f"{separator}{note}" if note else "")


def queue(conn: sqlite3.Connection, config: ProjectConfig, issue: Issue, text: str, at: datetime) -> None:
    if not enabled(config) or (issue.github is None and issue.is_closed):
        return
    github_mirror.queue_comment(conn, issue.id, text, at)
