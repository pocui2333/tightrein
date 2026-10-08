"""日志解析方法(json_lines、regex)共用的部分：条目类型、按行切分、级别映射、时区与事件编号。

- 片段按行切分，空行跳过，行尾的回车去掉；条目的 position 为片段起点加上该行在 UTF-8 文本中的字节偏移；
- 级别按配置映射(原文写法 → 归一化级别，不区分大小写)，映射不到的行计入 unparsed；
- 不带时区的时间按配置的时区(IANA 名称)解释，再换成 UTC；
- 堆栈帧的写法与技术栈有关，这两个通用方法不解析，frames 为空。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tightrein.collect.common.source import SourceMisconfigured
from tightrein.collect.platform_errors.log_platform.chunks import Chunk

LEVELS = ("trace", "debug", "information", "warning", "error", "critical")


@dataclass(frozen=True)
class Frame:
    symbol: str  # 命名空间.类名.方法名
    file: str | None
    line: int | None
    is_project: bool


@dataclass(frozen=True)
class ExceptionInfo:
    type: str
    message: str


@dataclass(frozen=True)
class LogEntry:
    stream: str
    position: int  # 字节位置
    occurred_at: datetime  # UTC
    local_time: str | None  # 原文中的时间写法
    level: str  # 归一化级别
    raw_level: str
    category: str | None
    event_id: int | str | None
    message: str
    exception: ExceptionInfo | None
    raw: str
    frames: tuple[Frame, ...] = ()


@dataclass(frozen=True)
class ParseResult:
    entries: list[LogEntry]
    state: dict[str, Any]  # 下次原样交回(只有时刻的写法靠它补日期)
    unparsed: int = 0
    notes: list[str] = field(default_factory=list)


def level_map(levels: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in levels.items()}


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise SourceMisconfigured(f"不认识的时区：{name}") from error


def to_utc(value: datetime, local: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=local)
    return value.astimezone(UTC).replace(microsecond=0)


def lines(chunk: Chunk) -> Iterator[tuple[int, str]]:
    """(字节位置, 行)，跳过空行。"""
    position = chunk.start_position
    for line in chunk.text.split("\n"):
        text = line.rstrip("\r")
        if text.strip():
            yield position, text
        position += len(line.encode("utf-8")) + 1


def event_id(value: Any) -> int | str | None:
    """整数与全为数字的字符串取整数，其余非空字符串原样保留(例如运行编号 `2026-09-28 run02_1130`)，空白串为空。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value) if value.isdigit() else value
    return None
