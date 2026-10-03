"""PR 状态跟踪(architecture/07 19.6)：只读查询 PR，已合并时 Issue 完成，关闭且未合并时以修复未采纳取消，仍打开时提醒与
冲突通知。PR 上的用户说明取最后一条评论或评审意见的正文。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from tightrein.domain.clock import workdays_between

MERGED = "MERGED"
CLOSED = "CLOSED"
OPEN = "OPEN"
CONFLICTING = "CONFLICTING"


@dataclass(frozen=True)
class PrUpdate:
    state: str
    note: str | None = None
    remind: bool = False
    conflict: bool = False


def user_note(pull: Any) -> str | None:
    items: list[Mapping[str, Any]] = [*pull.comments, *pull.reviews]
    bodies = [str(item.get("body") or "").strip() for item in items]
    bodies = [body for body in bodies if body]
    return bodies[-1] if bodies else None


def judge(pull: Any, created_at: datetime, last_reminded: datetime | None, now: datetime, workdays: int,
          non_working_days: frozenset[date]) -> PrUpdate:
    """PR 打开超过 workdays 个工作日(排除周末与 schedule.nonWorkingDays)时提醒，每天至多一次。"""
    if pull.state == MERGED:
        return PrUpdate(MERGED)
    if pull.state == CLOSED:
        return PrUpdate(CLOSED, user_note(pull))
    overdue = workdays_between(created_at.date(), now.date(), non_working_days) > workdays
    remind = overdue and (last_reminded is None or last_reminded.date() != now.date())
    return PrUpdate(OPEN, remind=remind, conflict=pull.mergeable == CONFLICTING)
