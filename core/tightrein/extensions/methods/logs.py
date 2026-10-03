"""日志条目的共用处理(architecture/10 3.5)：json-lines 与 regex 共用。

- 片段按行切分，空行跳过，行尾的回车去掉；条目的 position 为片段起点加上该行在 UTF-8 文本中的字节偏移。
  log-source 已把原文转为 UTF-8，来源编码不是 UTF-8 时这个位置只是近似值。
- 级别按 options.levels(原文写法 → 归一化级别，不区分大小写；缺省映射见 config/defaults.yaml)归一化为 trace、
  debug、information、warning、error、critical；映射不到的行计入 unparsed。
- 不带时区的时间按 options.timezone(IANA 名称)解释，再换算为 UTC。
- 堆栈帧的写法与技术栈有关，核心方法不解析，frames 为空；需要本项目帧时选用技术栈方法或以 extend 补充。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods.runtime import MethodError

def level_map(levels: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in levels.items()}


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"不认识的时区：{name}") from error


def to_utc(value: datetime, local: ZoneInfo) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=local)
    return format_iso(value.astimezone(timezone.utc))


def lines(chunk: Mapping[str, Any]) -> Iterator[tuple[int, str]]:
    """(字节位置, 行)，跳过空行。"""
    position = chunk["startPosition"]
    for line in chunk["text"].split("\n"):
        text = line.rstrip("\r")
        if text.strip():
            yield position, text
        position += len(line.encode("utf-8")) + 1


def entry(stream: str, position: int, occurred_at: str, local_time: str | None, level: str, raw_level: str,
          category: str | None, event_id: int | None, message: str, exception: Mapping[str, str] | None,
          raw: str) -> dict[str, Any]:
    return {"stream": stream, "position": position, "occurredAt": occurred_at, "localTime": local_time,
            "level": level, "rawLevel": raw_level, "category": category, "eventId": event_id, "message": message,
            "exception": None if exception is None else dict(exception), "frames": [], "raw": raw}


def event_id(value: Any) -> int | str | None:
    """事件编号：整数与全为数字的字符串取整数，其余非空字符串原样保留(例如运行编号 `2026-09-28 run02_1130`)。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value) if value.isdigit() else value
    return None
