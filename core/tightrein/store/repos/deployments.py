"""deployments 表：检测到的 staging 部署，每个 commit 一行。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.enums import DeploymentStatus
from tightrein.store.repos.table import TIME, Table, enum_codec, given


@dataclass(frozen=True)
class Deployment:
    commit: str
    status: DeploymentStatus
    detected_at: datetime
    workflow_run_id: str | None = None
    url: str | None = None
    deployed_at: datetime | None = None


TABLE = Table(
    "deployments",
    Deployment,
    ("commit",),
    {"status": enum_codec(DeploymentStatus), "detected_at": TIME, "deployed_at": TIME},
    order_by='detected_at, "commit"',
)


def save(conn: sqlite3.Connection, deployment: Deployment) -> None:
    TABLE.save(conn, deployment)


def get(conn: sqlite3.Connection, commit: str) -> Deployment | None:
    return TABLE.get(conn, commit=commit)


def find(conn: sqlite3.Connection, *, status: DeploymentStatus | None = None) -> list[Deployment]:
    """按检测时间升序。"""
    return TABLE.find(conn, **given({"status": status}))
