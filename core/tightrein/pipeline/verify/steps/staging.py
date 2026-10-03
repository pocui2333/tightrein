"""部署后确认的前置判断：找到包含合并提交的部署、等待是否超期。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from tightrein.domain.enums import DeploymentStatus
from tightrein.store.repos import deployments
from tightrein.store.repos.deployments import Deployment


class AncestryReader(Protocol):
    def is_ancestor(self, repo: Path, commit: str, of: str) -> bool: ...


def deployment_for(conn: sqlite3.Connection, git: AncestryReader, repo: Path, merge_commit: str) -> Deployment | None:
    """最早一次包含合并提交的成功部署。"""
    for deployment in deployments.find(conn, status=DeploymentStatus.SUCCEEDED):
        if deployment.commit == merge_commit or git.is_ancestor(repo, merge_commit, deployment.commit):
            return deployment
    return None


def wait_exceeded(deployed_at: datetime | None, now: datetime, days: int) -> bool:
    return deployed_at is not None and now - deployed_at > timedelta(days=days)
