"""日志平台取回的条目 → 信号：带堆栈的异常与错误级别的日志。

| 字段 | 取值 |
|---|---|
| check_type | error |
| location | 有本项目帧时为第一帧的「类名.方法名」，帧带文件时写成「文件:类名.方法名」；没有时为日志类别 |
| symbol | 第一个本项目帧的「类名.方法名」 |
| message | 「异常类型: 消息」；没有异常时为日志消息 |
| occurred_at | 条目的时间 |
| commit | 发生时间之前最近一次成功部署的 commit |

帧符号「命名空间.类名.方法名」只保留「类名.方法名」。没有平台分组编号，指纹由去重按异常类型与帧(或类别与消息)计算。
"""

from __future__ import annotations

from collections.abc import Iterable

from tightrein.collect.common.signals import ReleaseAt, Signal, SignalFactory
from tightrein.collect.platform_errors.log_parse.entries import Frame, LogEntry
from tightrein.collect.platform_errors.select import SelectedEntry

SOURCE_NAME = "log_platform"
CHECK_TYPE = "error"
NO_CATEGORY = "(无类别)"


def to_signals(items: Iterable[SelectedEntry], factory: SignalFactory, release_at: ReleaseAt) -> list[Signal]:
    signals = []
    for item in items:
        entry = item.entry
        signals.append(factory.create(
            check_type=CHECK_TYPE, location=location(item), message=message(entry),
            symbol=short_symbol(item.project_frames[0].symbol) if item.project_frames else None,
            occurred_at=entry.occurred_at, commit=release_at(entry.occurred_at),
            evidence={
                "sourceName": SOURCE_NAME,
                "level": entry.level,
                "rawLevel": entry.raw_level,
                "category": entry.category,
                "eventId": entry.event_id,
                "exceptionType": None if entry.exception is None else entry.exception.type,
                "projectFrames": [{"symbol": short_symbol(frame.symbol), "file": frame.file, "line": frame.line}
                                  for frame in item.project_frames],
                "excerpt": factory.excerpt(entry.raw),
                "logStream": entry.stream,
                "offset": entry.position,
                "serverLocalTime": entry.local_time,
            },
        ))
    return signals


def short_symbol(symbol: str) -> str:
    """命名空间.类名.方法名 → 类名.方法名；只有两段或更少时原样返回。"""
    parts = symbol.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else symbol


def location(item: SelectedEntry) -> str:
    if item.project_frames:
        return frame_location(item.project_frames[0])
    return item.entry.category or NO_CATEGORY


def frame_location(frame: Frame) -> str:
    symbol = short_symbol(frame.symbol)
    return f"{frame.file}:{symbol}" if frame.file else symbol


def message(entry: LogEntry) -> str:
    if entry.exception is not None:
        return f"{entry.exception.type}: {entry.exception.message}"
    return entry.message or entry.raw
