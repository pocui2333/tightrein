"""事件结构与写入(architecture/01 6.2，design 10.5)：一个事件一行 JSON。

- 写入 `WorkspaceLayout.events_log(日期)`：平时为 `data/logs/events-<日期>.jsonl`，`--output` 模式为
  `<输出目录>/events.jsonl`，不写 `data/logs/`；日期取事件时间的 UTC 日期。
- 写入前经过 Redactor：文本字段与 attributes 脱敏；编号、时间、工具与模型名和数值不处理，避免把 trace 编号误判为手机号。
- 写入失败不抛出，原因记入 `EventLog.failures`，由运行摘要报告「日志写入失败」。
- 每行以追加模式用一次 `os.write` 写入，多个进程同时写同一文件时行与行不会交错。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tightrein.domain.clock import format_iso, parse_iso
from tightrein.observability.redact import Redactor
from tightrein.store.files.layout import WorkspaceLayout

SPAN_OPERATIONS = ("invoke_agent", "execute_tool", "run_script")
INSTANT_OPERATIONS = ("gate", "user_action")
OPERATIONS = SPAN_OPERATIONS + INSTANT_OPERATIONS
REDACTED_FIELDS = ("status", "error_type", "decision", "reason", "artifact")

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def is_trace_id(value: str) -> bool:
    """32 位小写十六进制且不全为 0，与 OpenTelemetry 一致。"""
    return bool(TRACE_ID.match(value)) and set(value) != {"0"}


def is_span_id(value: str) -> bool:
    return bool(SPAN_ID.match(value)) and set(value) != {"0"}


@dataclass(frozen=True)
class Event:
    """一个事件。span 的 timestamp 为开始时间、duration_ms 为耗时；瞬时事件没有耗时。

    score 为该步骤的评分结果(design 12.3：评分项、结果、评分方式)，attributes 为各操作特有的补充信息，都是 JSON 对象。
    """

    timestamp: datetime
    run_id: str | None
    trace_id: str
    span_id: str
    parent_span_id: str | None
    stage: str | None
    operation: str
    agent: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    status: str | None = None
    error_type: str | None = None
    decision: str | None = None
    reason: str | None = None
    score: Mapping[str, Any] | None = None
    artifact: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("事件时间需要带时区")
        if self.operation not in OPERATIONS:
            raise ValueError(f"operation 只能是 {', '.join(OPERATIONS)}：{self.operation}")
        if not is_trace_id(self.trace_id):
            raise ValueError(f"trace_id 须为 32 位十六进制：{self.trace_id}")
        for name in ("span_id", "parent_span_id"):
            value = getattr(self, name)
            if value is not None and not is_span_id(value):
                raise ValueError(f"{name} 须为 16 位十六进制：{value}")

    def to_dict(self) -> dict[str, Any]:
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        data["timestamp"] = format_iso(self.timestamp)
        data["score"] = None if self.score is None else dict(self.score)
        data["attributes"] = dict(self.attributes)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"事件中有不认识的字段：{', '.join(unknown)}")
        return cls(**{**data, "timestamp": parse_iso(data["timestamp"]), "attributes": data.get("attributes") or {}})

    def redacted(self, redactor: Redactor) -> Event:
        changes: dict[str, Any] = {
            name: redactor.text(getattr(self, name)) for name in REDACTED_FIELDS if getattr(self, name) is not None
        }
        if self.score is not None:
            changes["score"] = redactor.value(dict(self.score))
        return replace(self, **changes, attributes=redactor.value(dict(self.attributes)))


def encode(event: Event) -> str:
    """一行 JSON，保留中文，不含换行。"""
    return json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str)


def read(path: Path) -> tuple[Event, ...]:
    """读取一个事件日志文件；某行不合格时抛出 ValueError 并给出行号。"""
    events: list[Event] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(Event.from_dict(json.loads(line)))
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"{path} 第 {number} 行不是合法的事件：{error}") from error
    return tuple(events)


@dataclass(frozen=True)
class WriteFailure:
    path: Path
    reason: str


class EventLog:
    """事件日志的写入端。写入失败只记录不抛出(architecture/01 6.2)。"""

    def __init__(self, layout: WorkspaceLayout, redactor: Redactor) -> None:
        self.layout = layout
        self.redactor = redactor
        self.failures: list[WriteFailure] = []

    def path_for(self, event: Event) -> Path:
        return self.layout.events_log(event.timestamp.astimezone(timezone.utc).date())

    def write(self, event: Event) -> bool:
        path = self.path_for(event)
        try:
            line = (encode(event.redacted(self.redactor)) + "\n").encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                written = os.write(descriptor, line)
            finally:
                os.close(descriptor)
            if written != len(line):
                raise OSError(f"只写入了 {written} / {len(line)} 字节")
        except (OSError, TypeError, ValueError) as error:
            self.failures.append(WriteFailure(path, f"{type(error).__name__}: {error}"))
            return False
        return True
