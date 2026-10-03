"""迁移检查与确认(architecture/07 11.3)：只从 diff 判断，不连接数据库，不读取任何配置文件与凭证。

改动文件匹配启动计划的 migrationPaths 时，启动服务会把新的迁移应用到测试库：提取新增的迁移条目，连同修复交接文档中
的「能否撤销与撤销方式」生成 local-migration 待确认操作(前置条件记录迁移文件的哈希)，本次验证停下；用户确认后重跑，
迁移文件哈希一致时才启动。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import OperationKind, OperationStatus, Stage
from tightrein.guards.protected import matches_path
from tightrein.store.repos import pending_operations
from tightrein.vcs import operations
from tightrein.vcs.operations import PendingOperation


def changed(files: Sequence[str], patterns: Sequence[str]) -> list[str]:
    return [path for path in files if any(matches_path(path, pattern) for pattern in patterns)]


def entries(diff_added: Mapping[str, Sequence[str]], files: Sequence[str]) -> list[str]:
    """迁移文件中新增的非空行。"""
    return [line.strip() for path in files for line in diff_added.get(path, ()) if line.strip()]


def files_hash(worktree: Path, files: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8") + b"\0")
        target = worktree / path
        digest.update(target.read_bytes() if target.is_file() else b"")
    return digest.hexdigest()


def request(conn: sqlite3.Connection, clock: Clock, *, repo: str, issue_id: str, files: Sequence[str],
            added: Sequence[str], migration: Mapping[str, Any] | None, sha: str) -> PendingOperation:
    revert = "修复计划没有说明撤销方式" if migration is None else (
        f"{'可以' if migration['reversible'] else '不能'}撤销：{migration['revertMethod']}")
    text = ("启动修复后的服务会把下列新增的迁移应用到测试库：\n" + "\n".join(f"- {line}" for line in added)
            + f"\n迁移文件：{'、'.join(files)}\n{revert}")
    return operations.confirmation(conn, clock, stage=Stage.VERIFY, subject_id=issue_id,
                                   kind=OperationKind.LOCAL_MIGRATION, repo=repo, text=text,
                                   impact="确认后启动服务，测试库中会执行这些迁移",
                                   preconditions={"migrationSha256": sha}, key=f"migration:{issue_id}:{sha}")


def confirmed(conn: sqlite3.Connection, issue_id: str, sha: str) -> bool:
    return any(record.kind is OperationKind.LOCAL_MIGRATION and record.preconditions.get("migrationSha256") == sha
               for record in pending_operations.find(conn, subject_id=issue_id, status=OperationStatus.EXECUTED))
