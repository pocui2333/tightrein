"""onboarding_items 表：接入清单各项的状态、说明、推荐答案与用户的回答(orchestrator/onboarding)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.store.repos.table import TIME, Table


@dataclass(frozen=True)
class OnboardingItem:
    item: str
    position: int
    title: str
    state: str          # done、blocked(需要用户回答)、failed、pending
    owner: str          # system、user
    detail: str
    updated_at: datetime
    recommendation: str | None = None
    answer: str | None = None


TABLE = Table("onboarding_items", OnboardingItem, ("item",), {"updated_at": TIME}, order_by="position, item")


def all_items(conn: sqlite3.Connection) -> list[OnboardingItem]:
    return TABLE.find(conn)


def get(conn: sqlite3.Connection, item: str) -> OnboardingItem | None:
    return TABLE.get(conn, item=item)


def save(conn: sqlite3.Connection, item: OnboardingItem) -> None:
    TABLE.save(conn, item)


def remove_except(conn: sqlite3.Connection, keep: set[str]) -> None:
    """删去本次清单中已不存在的项(配置改动后不再适用的检查)。"""
    for found in TABLE.find(conn):
        if found.item not in keep:
            TABLE.delete(conn, item=found.item)
