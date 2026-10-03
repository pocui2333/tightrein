"""issue_events 表：Issue 文件「历史」一节的结构化副本，只追加。event 为 IssueEvent 的取值或 user-edited。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.enums import CloseReason, IssueEvent, IssueStatus
from tightrein.store.repos.table import TIME, Table, enum_codec

USER_EDITED = "user-edited"
EVENTS = frozenset({member.value for member in IssueEvent} | {USER_EDITED})


@dataclass(frozen=True)
class IssueEventRecord:
    issue_id: str
    at: datetime
    event: str
    actor: str
    from_status: IssueStatus | None = None
    to_status: IssueStatus | None = None
    close_reason: CloseReason | None = None
    note: str | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        if self.event not in EVENTS:
            raise ValueError(f"Issue 事件只能是 IssueEvent 的取值或 {USER_EDITED}：{self.event}")


TABLE = Table(
    "issue_events",
    IssueEventRecord,
    ("id",),
    {
        "at": TIME,
        "from_status": enum_codec(IssueStatus),
        "to_status": enum_codec(IssueStatus),
        "close_reason": enum_codec(CloseReason),
    },
    order_by="at, id",
    generated=("id",),
)


def append(conn: sqlite3.Connection, record: IssueEventRecord) -> int:
    if record.id is not None:
        raise ValueError("追加的事件不能自带编号")
    return TABLE.insert(conn, record)


def for_issue(conn: sqlite3.Connection, issue_id: str) -> list[IssueEventRecord]:
    return TABLE.find(conn, issue_id=issue_id)
