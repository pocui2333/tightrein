"""日志平台取回的条目到信号的映射(redesign/01-collect.md 第 1 节)：带堆栈的异常与错误级别的日志。

| 字段 | 取值 |
|---|---|
| source | error |
| check | 条目的归一化级别：error 或 critical |
| location | 有本项目帧时为第一帧的「类名.方法名」，帧中带文件时写成「文件:类名.方法名」；没有时为日志类别 |
| message | 「异常类型: 消息」；没有异常时为日志消息 |
| occurred_at | 条目的 occurredAt |
| release | 发生时间之前最近一次成功部署的 commit(由调用方给出的函数查询) |
| actor | 空 |

帧的 symbol 为「命名空间.类名.方法名」，写入 projectFrames 与 location 时去掉命名空间只保留「类名.方法名」。
context.sourceName 为 log-platform(问题范围的来源，覆盖运行据此判断)。指纹按逻辑位置计算(没有平台分组编号)。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import Source
from tightrein.domain.signal import Signal
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import SignalFactory
from tightrein.sources.platform_errors.select import SelectedEntry

NO_CATEGORY = "(无类别)"
SOURCE_NAME = "log-platform"

ReleaseAt = Callable[[datetime], str | None]


def short_symbol(symbol: str) -> str:
    """命名空间.类名.方法名 → 类名.方法名；只有两段或更少时原样返回。"""
    parts = symbol.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else symbol


def frame_location(frame: Mapping[str, Any]) -> str:
    symbol = short_symbol(frame["symbol"])
    return f"{frame['file']}:{symbol}" if frame.get("file") else symbol


def location(item: SelectedEntry) -> str:
    if item.project_frames:
        return frame_location(item.project_frames[0])
    return item.entry.get("category") or NO_CATEGORY


def message(entry: Mapping[str, Any]) -> str:
    exception = entry.get("exception")
    if exception:
        return f"{exception['type']}: {exception['message']}"
    return entry.get("message") or entry.get("raw") or ""


def to_signals(items: Iterable[SelectedEntry], factory: SignalFactory, redactor: ProbeRedactor,
               release_at: ReleaseAt) -> list[Signal]:
    signals = []
    for item in items:
        entry = item.entry
        occurred = parse_iso(entry["occurredAt"])
        exception = entry.get("exception")
        frames = [{"symbol": short_symbol(frame["symbol"]), "file": frame.get("file"), "line": frame.get("line")}
                  for frame in item.project_frames]
        signals.append(factory.create(
            source=Source.ERROR, check=entry["level"], location=location(item), message=message(entry),
            occurred_at=occurred, release=release_at(occurred),
            context={
                "sourceName": SOURCE_NAME,
                "category": entry.get("category"),
                "eventId": entry.get("eventId"),
                "exceptionType": None if not exception else exception["type"],
                "projectFrames": frames,
                "excerpt": redactor.excerpt(entry.get("raw") or "", redactor.limits.log_excerpt_chars),
                "logStream": entry["stream"],
                "offset": entry["position"],
                "serverLocalTime": entry.get("localTime"),
                "rawLevel": entry.get("rawLevel"),
            },
        ))
    return signals
