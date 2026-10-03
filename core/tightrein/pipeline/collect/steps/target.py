"""第 1 步 目标解析(architecture/05 2.5)：地址、目标版本、只读 worktree。

目标版本缺省取最近一次成功部署的 commit(部署来源见 pipeline/common/deploys.py)，并以 commit 为键写入 deployments；
没有配置部署来源时为空，由 collect 在运行说明中写明。static 审查的是主分支上的新提交，目标版本取 origin/<主分支>。--commit 覆盖目标版本，
--target 只替换地址；两者都没有且没有配置 target 时地址为空。环境名取 target.environment(缺省 staging)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import DeploymentStatus
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.sources.base import ProbeTarget
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import deployments
from tightrein.store.repos.deployments import Deployment
from tightrein.pipeline.common.deploys import DeployRecord
from tightrein.vcs.errors import RefNotFound

HEAD = "HEAD"


class DeploymentReader(Protocol):
    def latest_success(self) -> DeployRecord | None: ...


class RefReader(Protocol):
    def rev_parse(self, repo: Path, ref: str) -> str: ...


@dataclass(frozen=True)
class TargetInfo:
    environment: str
    base_url: str | None
    release: str | None
    worktree: Path | None
    worktree_head: str | None

    def probe_target(self, run_id: str, raw_dir: Path, clock: Clock) -> ProbeTarget:
        return ProbeTarget(environment=self.environment, run_id=run_id, raw_dir=raw_dir, clock=clock,
                           base_url=self.base_url, release=self.release, worktree=self.worktree)


def detect_deployment(conn: sqlite3.Connection, reader: DeploymentReader, clock: Clock, *,
                      record: bool) -> Deployment | None:
    """最近一次成功部署；record 为真时按 commit 写入或更新 deployments(保留首次检测的时间)。"""
    found = reader.latest_success()
    if found is None:
        return None
    existing = deployments.get(conn, found.commit)
    deployment = Deployment(
        commit=found.commit, status=DeploymentStatus.SUCCEEDED,
        detected_at=existing.detected_at if existing is not None else clock.now(),
        workflow_run_id=found.id, url=found.url, deployed_at=found.created_at,
    )
    if record:
        deployments.save(conn, deployment)
    return deployment


def _head(refs: RefReader, worktree: Path) -> str | None:
    if not worktree.is_dir():
        return None
    try:
        return refs.rev_parse(worktree, HEAD)
    except RefNotFound:
        return None


def resolve(config: ProjectConfig, layout: WorkspaceLayout, conn: sqlite3.Connection, probe: ProbeKind, *,
            deployments_reader: DeploymentReader, refs: RefReader, clock: Clock, target: str | None = None,
            commit: str | None = None, record: bool = True) -> TargetInfo:
    worktree = layout.readonly_worktree()
    release = commit
    if release is None and probe is ProbeKind.STATIC:
        release = refs.rev_parse(config.repo, f"origin/{config.main_branch}")
    elif release is None:
        deployment = detect_deployment(conn, deployments_reader, clock, record=record)
        release = deployment.commit if deployment is not None else None
    return TargetInfo(
        environment=str(config.get("target.environment")),
        base_url=target or config.base_url,
        release=release,
        worktree=worktree if worktree.is_dir() else None,
        worktree_head=_head(refs, worktree),
    )
