"""从评估与实施的交接文档(handoff.json)取出任务外发现。

- 只读 controls."collect.incidental".points 中列出的步骤(及其下的小步骤)的交接文档，按文件名先筛，不逐个打开；
- 路径和内容哈希都与已读记录相同的跳过；交接文档重跑后内容变了就按新内容重读，重复的发现由去重按指纹归并；
- 不论这一步的结论是否通过都取发现：审查不通过(有阻断项)的轮次里顺带发现的问题同样是真问题，修正轮次只审新改的
  文件，不收就丢了；发现已由调用时的 schema 校验过，重复的由去重按指纹归并；
- 读不了或解析不了(不是合法的交接文档、发现不合 finding.schema.json)的单个文件跳过、不记已读，下次重试；
- 发现的 commit 取产生它的环节的基准(points 中给出 facts 里的字段名)，occurred_at 取交接文档的生成时间。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.collect.incidental.mapping import Finding
from tightrein.protocol.handoff import Handoff, load_schema, schema_errors
from tightrein.protocol.naming import parse_iso
from tightrein.store.files.layout import WorkspaceLayout

FINDINGS_KEY = "incidentalFindings"
FINDING_SCHEMA = Path(__file__).with_name("finding.schema.json")
HANDOFF_SUFFIX = "-handoff.json"
_ROUND = re.compile(r"\.r\d+$")


@dataclass(frozen=True)
class SourceRead:
    path: str  # 相对工作区
    content_hash: str
    findings: tuple[Finding, ...] = ()
    error: str | None = None


def candidates(layout: WorkspaceLayout, points: Mapping[str, str]) -> list[tuple[Path, str]]:
    """(交接文档, 匹配到的步骤)，按路径排序。"""
    found = []
    for directory in (layout.problems_dir, layout.issues_dir):
        for path in sorted(directory.glob(f"*/*{HANDOFF_SUFFIX}")) if directory.is_dir() else []:
            matched = _matched(point_of(path.name), points)
            if matched is not None:
                found.append((path, matched))
    return found


def point_of(name: str) -> str:
    """`21-assess.triage-handoff.json` → `assess.triage`；带轮次的 `.r2` 去掉。"""
    middle = name.split("-", 1)[1][: -len(HANDOFF_SUFFIX)] if "-" in name else ""
    return _ROUND.sub("", middle)


def unread(layout: WorkspaceLayout, points: Mapping[str, str], known: Mapping[str, str],
           now: datetime) -> Iterator[tuple[str, SourceRead | None]]:
    """每个候选一项：(相对路径, 读到的内容)；与已读记录相同(没变)的为 None。"""
    schema = load_schema(FINDING_SCHEMA)
    for path, matched in candidates(layout, points):
        relative = path.relative_to(layout.root).as_posix()
        try:
            data = path.read_bytes()
        except OSError as error:
            yield relative, SourceRead(relative, "", error=f"{relative} 无法读取：{type(error).__name__}")
            continue
        digest = hashlib.sha256(data).hexdigest()
        if known.get(relative) == digest:
            yield relative, None
            continue
        yield relative, read(relative, digest, data, points[matched], schema, now)


def read(relative: str, digest: str, data: bytes, release_field: str, schema: dict[str, Any],
         now: datetime) -> SourceRead:
    try:
        document = Handoff.from_json(json.loads(data.decode("utf-8")))
        items = document.facts.get(FINDINGS_KEY) or []
        errors = [error for item in items for error in schema_errors(item, schema)]
        if errors:
            return SourceRead(relative, digest, error=f"{relative} 的任务外发现不合格式：{errors[0]}")
        occurred = parse_iso(document.created_at) if document.created_at else now
        commit = document.facts.get(release_field)
        findings = tuple(Finding(item["file"], item["line"], item["symbol"], item["category"], item["confidence"],
                                 item["evidence"], item["text"], relative, document.point, document.subject,
                                 document.run, occurred, commit if isinstance(commit, str) else None)
                         for item in items)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return SourceRead(relative, digest, error=f"{relative} 无法解析：{type(error).__name__}")
    return SourceRead(relative, digest, findings)


def _matched(point: str, points: Mapping[str, str]) -> str | None:
    return next((prefix for prefix in points if point == prefix or point.startswith(prefix + ".")), None)
