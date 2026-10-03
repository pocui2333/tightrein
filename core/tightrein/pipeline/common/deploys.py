"""部署跟踪的核心判断(redesign/07-release.md 第 4 节)：经扩展点 deploy-source 读取平台上最近的部署，按 git 祖先关系
找包含某个合并提交的部署；没有配置部署来源时以合并时间加观察期为准。采集、发布跟踪、编排与部署后确认共用。

包含合并提交的部署：先找 commit 相同的最新一次，它被跳过(skipped)或没有时，改找 commit 包含它的最早一次(等待窗口内的
多个提交由之后的一次部署统一部署)；部署的 commit 在本地不存在时先 fetch 一次再判断，仍不存在的不计入。
没有配置部署来源时，合并后经过 release.deploy.observationHours 视为已部署(source 为 merge-time)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import DeploymentStatus, ExtensionLayer, ExtensionPoint
from tightrein.vcs.errors import RefNotFound, VcsError

SKIPPED = "skipped"
SOURCE = "deploy-source"
MERGE_TIME = "merge-time"
STATUSES = {"running": DeploymentStatus.RUNNING, "succeeded": DeploymentStatus.SUCCEEDED,
            "failed": DeploymentStatus.FAILED}
UNCONFIGURED = "未配置部署来源(extensions.deploy-source)，以合并时间加观察期为准"


class DeploySourceError(VcsError):
    """部署来源读取失败(扩展失败或输出不合格)。"""


class AncestryReader(Protocol):
    def is_ancestor(self, repo: Path, commit: str, of: str) -> bool: ...

    def fetch(self, repo: Path, prune: bool = False) -> None: ...

    def head(self, repo: Path) -> Any: ...


@dataclass(frozen=True)
class DeployRecord:
    id: str
    commit: str
    status: str  # running、succeeded、failed、skipped
    environment: str | None
    url: str | None
    created_at: datetime

    @property
    def deployment_status(self) -> DeploymentStatus:
        return STATUSES.get(self.status, DeploymentStatus.FAILED)


@dataclass(frozen=True)
class Found:
    status: DeploymentStatus
    record: DeployRecord | None = None


class DeploySource:
    def __init__(self, extensions: Callable[[], Any], git: AncestryReader, repo: Path) -> None:
        """extensions 为构造 ExtensionClient 的函数(用到时才构造)。"""
        self._extensions = extensions
        self.git = git
        self.repo = repo

    def configured(self) -> bool:
        return self._extensions().resolution.get(ExtensionPoint.DEPLOY_SOURCE).layer is not ExtensionLayer.DEFAULT

    def records(self) -> list[DeployRecord]:
        """最近的部署，按创建时间从早到晚；没有配置部署来源时为空。"""
        if not self.configured():
            return []
        head = self.git.head(self.repo).commit
        if head is None:
            raise DeploySourceError(f"{self.repo} 没有提交，无法读取部署记录")
        result = self._extensions().deploy_source(self.repo, head)
        if result.failure is not None:
            raise DeploySourceError(f"读取部署记录失败：{result.failure.describe()}")
        if result.output is None:
            return []
        found = [DeployRecord(item["id"], item["commit"], item["status"], item.get("environment"), item.get("url"),
                              parse_iso(item["createdAt"])) for item in result.output["deployments"]]
        return sorted(found, key=lambda item: (item.created_at, item.id))

    def _contains(self, commit: str, record: DeployRecord, fetched: list[bool]) -> bool:
        try:
            return self.git.is_ancestor(self.repo, commit, record.commit)
        except RefNotFound:
            if fetched[0]:
                return False
            fetched[0] = True
            self.git.fetch(self.repo)
            try:
                return self.git.is_ancestor(self.repo, commit, record.commit)
            except RefNotFound:
                return False

    def find(self, commit: str) -> Found:
        """包含 commit 的部署；还没有时为 pending。"""
        records = self.records()
        exact = [record for record in records if record.commit == commit]
        if exact and exact[-1].status != SKIPPED:
            return Found(exact[-1].deployment_status, exact[-1])
        fetched = [False]
        for record in records:
            if record.status != SKIPPED and record.commit != commit and self._contains(commit, record, fetched):
                return Found(record.deployment_status, record)
        return Found(DeploymentStatus.PENDING)

    def latest_success(self) -> DeployRecord | None:
        succeeded = [record for record in self.records() if record.status == "succeeded"]
        return succeeded[-1] if succeeded else None


def observed_at(config: ProjectConfig, merged_at: datetime) -> datetime:
    """没有部署来源时视为已部署的时刻：合并时间加 release.deploy.observationHours。"""
    return merged_at + timedelta(hours=float(config.get("release.deploy.observationHours")))
