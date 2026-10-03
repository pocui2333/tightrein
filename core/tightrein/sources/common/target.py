"""目标版本的确定(architecture/04 1.3、4.4)。

- api-fuzz 的目标版本为 deployments 表中最近一次成功部署的 commit；
- 平台来源与项目探针的每条信号取发生时间之前最近一次成功部署的 commit；
- 部署时间取 deployed_at，没有时取 detected_at；两次部署时间相同时按 commit 排序，结果稳定。
- static 与只读 worktree 的比较：worktree 的 HEAD 须与目标 commit 一致(允许以缩写前缀给出目标)。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from tightrein.domain.enums import DeploymentStatus
from tightrein.store.repos import deployments
from tightrein.store.repos.deployments import Deployment


def _deployed(deployment: Deployment) -> datetime:
    return deployment.deployed_at or deployment.detected_at


def _succeeded(conn: sqlite3.Connection) -> list[Deployment]:
    found = deployments.find(conn, status=DeploymentStatus.SUCCEEDED)
    return sorted(found, key=lambda item: (_deployed(item), item.commit))


def latest_release(conn: sqlite3.Connection) -> str | None:
    found = _succeeded(conn)
    return found[-1].commit if found else None


def release_at(conn: sqlite3.Connection, at: datetime) -> str | None:
    found = [item for item in _succeeded(conn) if _deployed(item) <= at]
    return found[-1].commit if found else None


def head_matches(head: str | None, commit: str) -> bool:
    return head is not None and bool(commit) and head.startswith(commit)
