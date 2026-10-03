"""从分诊与修复的交接文档读取任务外发现(architecture/04 6.2)。

查询 handoffs 表中环节为 triage 或 fix、状态为 ok、未标记过期的记录，读取交接文档的 outputs.incidentalFindings：
每项带结构化位置(file、line、symbol)与原文，按 common.schema.json 的 incidentalFinding 校验。
- 路径与内容哈希都与 incidental_sources 中的记录相同的来源跳过；交接文档重跑后内容变化，按新内容重新读取，
  重复的发现由 aggregate 按指纹归并；
- 单个来源读取或解析失败时跳过该来源、不写已读记录(下次重试)，原因返回给调用方写入 notes。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import HandoffStatus, RunStage
from tightrein.sources.incidental.locate import Location
from tightrein.sources.incidental.mapping import Finding
from tightrein.store.repos import handoffs, incidental_sources
from tightrein.store.repos.handoffs import HandoffRecord

STAGES = (RunStage.TRIAGE, RunStage.FIX)
RELEASE_FIELDS = {RunStage.TRIAGE: "triageCommit", RunStage.FIX: "baseCommit"}


@dataclass(frozen=True)
class SourceRead:
    path: str
    content_hash: str
    findings: tuple[Finding, ...] = ()
    error: str | None = None


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def candidates(conn: sqlite3.Connection) -> list[HandoffRecord]:
    rows = conn.execute(
        "SELECT * FROM handoffs WHERE stage IN (?, ?) AND status = ? AND stale_at IS NULL "
        "ORDER BY created_at, stage, subject_id, attempt",
        (RunStage.TRIAGE.value, RunStage.FIX.value, HandoffStatus.OK.value),
    ).fetchall()
    return [handoffs.TABLE.from_row(row) for row in rows]


def _finding(item: Mapping[str, Any], record: HandoffRecord, document: Mapping[str, Any]) -> Finding:
    outputs = document["outputs"]
    return Finding(
        text=item["text"], location=Location(item["file"], item.get("line"), item.get("symbol")),
        source_path=record.path, stage=record.stage.value, occurred_at=parse_iso(document["createdAt"]),
        release=outputs.get(RELEASE_FIELDS[record.stage]), subject=document["subject"]["id"],
        run_id=document["runId"],
    )


def read(root: Path, record: HandoffRecord) -> SourceRead:
    path = root / record.path
    try:
        data = path.read_bytes()
    except OSError as error:
        return SourceRead(record.path, "", error=f"{record.path} 无法读取：{type(error).__name__}")
    digest = content_hash(data)
    try:
        document = json.loads(data.decode("utf-8"))
        items = document["outputs"].get("incidentalFindings", [])
        for item in items:
            validate.check("common.schema.json", item, definition="incidentalFinding")
        findings = tuple(_finding(item, record, document) for item in items)
    except (ValueError, KeyError, TypeError, AttributeError, SchemaValidationError) as error:
        return SourceRead(record.path, digest, error=f"{record.path} 无法解析：{type(error).__name__}")
    return SourceRead(record.path, digest, findings)


def unread(conn: sqlite3.Connection, root: Path) -> list[SourceRead]:
    """尚未读取或内容已变化的交接文档。"""
    results = []
    for record in candidates(conn):
        result = read(root, record)
        known = incidental_sources.get(conn, record.path)
        if result.error is None and known is not None and known.content_hash == result.content_hash:
            continue
        results.append(result)
    return results
