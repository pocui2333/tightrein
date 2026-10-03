"""github_mirror 与 github_mirror_comments 表：GitHub Issue 镜像的簿记与关键节点的待发评论(architecture/06 10.8)。

镜像编号与链接属于 Issue(文件头信息与 issues 表)；这里只记 GitHub 上已知的开关状态、已打的状态与类型标签、已建立的
子 Issue 与阻塞关系与最近一次失败，不随 reindex 重建。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime

from tightrein.store.repos.table import TIME, Table

OPEN = "open"
CLOSED = "closed"


@dataclass(frozen=True)
class MirrorState:
    issue_id: str
    remote_state: str | None = None
    labelled_status: str | None = None
    error: str | None = None
    failed_at: datetime | None = None
    synced_at: datetime | None = None
    relations: str | None = None  # 已建立的子 Issue 与阻塞关系，如 parent=12;blocked-by=13


@dataclass(frozen=True)
class MirrorComment:
    id: int | None
    issue_id: str
    body: str
    created_at: datetime
    posted_at: datetime | None = None


STATES = Table("github_mirror", MirrorState, ("issue_id",), {"failed_at": TIME, "synced_at": TIME},
               order_by="issue_id")
COMMENTS = Table("github_mirror_comments", MirrorComment, ("id",), {"created_at": TIME, "posted_at": TIME},
                 order_by="id", generated=("id",))


def get(conn: sqlite3.Connection, issue_id: str) -> MirrorState:
    """没有记录时返回空白的簿记(还没有镜像)。"""
    return STATES.get(conn, issue_id=issue_id) or MirrorState(issue_id)


def save(conn: sqlite3.Connection, state: MirrorState) -> None:
    STATES.save(conn, state)


def find(conn: sqlite3.Connection) -> list[MirrorState]:
    return STATES.find(conn)


def record_failure(conn: sqlite3.Connection, issue_id: str, error: str, at: datetime) -> None:
    save(conn, replace(get(conn, issue_id), error=error, failed_at=at))


def queue_comment(conn: sqlite3.Connection, issue_id: str, body: str, at: datetime) -> int:
    return COMMENTS.insert(conn, MirrorComment(None, issue_id, body, at))


def unposted(conn: sqlite3.Connection, issue_id: str | None = None) -> list[MirrorComment]:
    """还没有发出的评论，按入队顺序；不给 issue_id 时为全部。"""
    if issue_id is None:
        return COMMENTS.find(conn, posted_at=None)
    return COMMENTS.find(conn, issue_id=issue_id, posted_at=None)


def mark_posted(conn: sqlite3.Connection, comment_id: int, at: datetime) -> None:
    conn.execute("UPDATE github_mirror_comments SET posted_at = ? WHERE id = ?", (TIME.to_column(at), comment_id))
