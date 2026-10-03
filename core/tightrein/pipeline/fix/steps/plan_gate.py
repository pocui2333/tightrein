"""计划确认(architecture/07 4.7)：修复中唯一的用户关口。

计划的 JSON 存为 plan.json(重出时旧版本改名保留为 plan.<序号>.json)，渲染为 plan.md；生成 kind 为 fix-plan 的
待确认操作，前置条件记录 plan.json 的哈希，同一 Issue 未执行的旧 fix-plan 操作改为 expired。确认后在
confirmation.json 中记录操作编号、时间、计划哈希与用户的说明；apply 要求这份记录的哈希与当前计划一致。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import OperationKind, OperationStatus, Stage
from tightrein.store.files import atomic
from tightrein.store.repos import pending_operations
from tightrein.vcs import operations
from tightrein.vcs.operations import PendingOperation

PLAN = "plan.json"
PLAN_MD = "plan.md"
CONFIRMATION = "confirmation.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(directory: Path, plan: Mapping[str, Any], markdown: str) -> Path:
    path = directory / PLAN
    if path.exists():
        number = 1
        while (directory / f"plan.{number}.json").exists():
            number += 1
        path.rename(directory / f"plan.{number}.json")
        (directory / CONFIRMATION).unlink(missing_ok=True)
    atomic.write_text(path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    atomic.write_text(directory / PLAN_MD, markdown)
    return path


def load(directory: Path) -> dict[str, Any] | None:
    path = directory / PLAN
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def pending(conn: sqlite3.Connection, issue_id: str) -> list[PendingOperation]:
    return [PendingOperation.from_record(record)
            for record in pending_operations.find(conn, subject_id=issue_id, status=OperationStatus.PENDING)
            if record.kind is OperationKind.FIX_PLAN]


def request(conn: sqlite3.Connection, clock: Clock, *, repo: str, issue_id: str, plan_sha: str,
            text: str) -> PendingOperation:
    key = f"fix-plan:{issue_id}:{plan_sha}"
    for operation in pending(conn, issue_id):
        if operation.idempotency_key != key:
            operations.save(conn, replace(operation, status=OperationStatus.EXPIRED,
                                          result={"reason": "计划已重出，需要确认新的计划"}))
    return operations.confirmation(conn, clock, stage=Stage.FIX, subject_id=issue_id, kind=OperationKind.FIX_PLAN,
                                   repo=repo, text=text, impact="确认后 fix apply 按这份计划修改修复 worktree 中的代码",
                                   preconditions={"planSha256": plan_sha}, key=key)


def record(directory: Path, operation_id: str | None, plan_sha: str, clock: Clock, note: str | None = None) -> None:
    atomic.write_text(directory / CONFIRMATION, json.dumps(
        {"operationId": operation_id, "planSha256": plan_sha, "confirmedAt": format_iso(clock.now()), "note": note},
        ensure_ascii=False, indent=2) + "\n")


def confirmed(directory: Path) -> dict[str, Any] | None:
    """已确认且计划没有变化时返回确认记录。"""
    path = directory / CONFIRMATION
    if not path.is_file() or not (directory / PLAN).is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if data["planSha256"] == sha256(directory / PLAN) else None
