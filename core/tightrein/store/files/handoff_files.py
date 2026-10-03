"""交接文档的写入、覆盖与历史保留(design 10.2、15.7)。

文件名由模块与对象决定(architecture/01 1.1)：`<环节>-<对象编号>`，验证为 `verify-<阶段>-<Issue 编号>`，
周报为 `learn-weekly-<日期>`。同一运行中重写同一份交接文档时，旧文件先复制为 `<文件名>.<序号>.json` 再覆盖，
序号从 1 递增；handoffs 表按(环节、阶段、对象、次数)更新，不插入重复记录。写入前按 validate_handoff 校验，
不合格的文档不落盘。`--output` 模式只写文件，不写数据库。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from tightrein.contracts import versions
from tightrein.contracts.validate import SchemaValidationError, validate, validate_handoff
from tightrein.domain import ids
from tightrein.domain.clock import Clock
from tightrein.domain.enums import HandoffStatus, RunStage, VerifyPhase
from tightrein.store.files import atomic
from tightrein.store.files.layout import HANDOFF_SUFFIX, WorkspaceLayout
from tightrein.store.repos import handoffs
from tightrein.store.repos.handoffs import HandoffRecord

ENVELOPE = "handoff/envelope.schema.json"
WEEK_SUBJECT = "week"


class HandoffError(ValueError):
    """交接文档的版本、验证阶段或写入方式不符合约定。"""


@dataclass(frozen=True)
class WrittenHandoff:
    path: Path
    previous: Path | None
    record: HandoffRecord | None


def handoff_name(document: dict[str, Any], phase: VerifyPhase | None = None) -> str:
    stage, subject = document["stage"], document["subject"]
    if stage == RunStage.VERIFY.value:
        if phase is None:
            raise HandoffError("verify 的交接文档需要验证阶段")
        return ids.verify_handoff_id(phase.value, subject["id"])
    if phase is not None:
        raise HandoffError(f"只有 verify 的交接文档带验证阶段，当前为 {stage}")
    if subject["type"] == WEEK_SUBJECT:
        return ids.weekly_handoff_id(date.fromisoformat(subject["id"]))
    return ids.handoff_id(stage, subject["id"])


def _numbered(layout: WorkspaceLayout, run_id: str, name: str) -> list[tuple[int, Path]]:
    pattern = re.compile(rf"^{re.escape(name)}\.(\d+){re.escape(HANDOFF_SUFFIX)}$")
    directory = layout.handoff_dir(run_id)
    if not directory.is_dir():
        return []
    return sorted(
        (int(match.group(1)), path)
        for path in directory.iterdir()
        if (match := pattern.match(path.name)) is not None
    )


def history(layout: WorkspaceLayout, run_id: str, name: str) -> list[Path]:
    """同一运行中这份交接文档的旧版本，按序号升序。"""
    return [path for _, path in _numbered(layout, run_id, name)]


def write(
    layout: WorkspaceLayout,
    document: dict[str, Any],
    clock: Clock,
    *,
    conn: sqlite3.Connection | None = None,
    phase: VerifyPhase | None = None,
    attempt: int = 1,
) -> WrittenHandoff:
    """校验后写入交接文档；conn 给出时同时更新 handoffs 表。"""
    errors = validate_handoff(document)
    if errors:
        raise SchemaValidationError(ENVELOPE, errors)
    current = versions.current(ENVELOPE)
    if document["schemaVersion"] != current:
        raise HandoffError(f"交接文档须按当前第 {current} 版写入，收到第 {document['schemaVersion']} 版")
    if conn is not None and layout.output_dir is not None:
        raise HandoffError("--output 模式不写数据库")
    name = handoff_name(document, phase)
    run_id = document["runId"]
    path = layout.handoff(run_id, name)
    previous = None
    if path.exists():
        numbered = _numbered(layout, run_id, name)
        previous = layout.handoff_history(run_id, name, numbered[-1][0] + 1 if numbered else 1)
        shutil.copyfile(path, previous)
    atomic.write_text(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    record = None
    if conn is not None:
        record = HandoffRecord(
            stage=RunStage(document["stage"]),
            subject_id=document["subject"]["id"],
            attempt=attempt,
            run_id=run_id,
            path=layout.relative(path),
            status=HandoffStatus(document["status"]),
            schema_version=current,
            created_at=clock.now(),
            phase=phase,
        )
        handoffs.save(conn, record)
    return WrittenHandoff(path, previous, record)


def read(path: Path) -> dict[str, Any]:
    """读取交接文档，旧版本逐级升级到当前版本后校验。"""
    document = json.loads(path.read_text(encoding="utf-8"))
    version = document.get("schemaVersion") if isinstance(document, dict) else None
    if isinstance(version, int) and 1 <= version < versions.current(ENVELOPE):
        document = versions.upgrade(ENVELOPE, document, version)
    errors = validate_handoff(document) if isinstance(document, dict) else validate(ENVELOPE, document)
    if errors:
        raise SchemaValidationError(ENVELOPE, errors)
    return document
