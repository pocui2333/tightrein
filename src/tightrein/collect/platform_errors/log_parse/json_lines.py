"""json_lines：每行一个 JSON 对象的结构化日志。

字段位置由参数给出，嵌套字段以 `.` 连接(如 `log.level`)：时间、级别、消息，以及可选的类别、事件编号、异常类型与
异常消息。时间可以是 ISO 8601 字符串，也可以是 Unix 时间戳(秒；大于 10^11 时按毫秒)；布尔值不当时间(bool 是 int
的子类)。不是 JSON 对象、没有可用的时间或级别的行计入 unparsed。不需要跨片段的状态，state 原样返回。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from tightrein.collect.platform_errors.log_parse import entries
from tightrein.collect.platform_errors.log_parse.entries import ExceptionInfo, LogEntry, ParseResult
from tightrein.collect.platform_errors.log_platform.chunks import Chunk

MILLISECOND_THRESHOLD = 10 ** 11
MILLISECONDS_PER_SECOND = 1000
_MISSING = object()


def parse(chunks: Sequence[Chunk], options: Mapping[str, Any], state: Mapping[str, Any] | None,
          now: datetime) -> ParseResult:
    local = entries.zone(options["timezone"])
    levels = entries.level_map(options["levels"])
    found: list[LogEntry] = []
    unparsed = 0
    for chunk in chunks:
        for position, line in entries.lines(chunk):
            entry = parse_line(chunk.stream, position, line, options, levels, local)
            if entry is None:
                unparsed += 1
            else:
                found.append(entry)
    return ParseResult(found, dict(state or {}), unparsed)


def parse_line(stream: str, position: int, line: str, options: Mapping[str, Any], levels: Mapping[str, str],
               local: ZoneInfo) -> LogEntry | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    raw_time = _field(record, options["timeField"])
    occurred_at = parse_time(raw_time, local)
    raw_level = _text(_field(record, options["levelField"]))
    level = None if not raw_level else levels.get(raw_level.lower())
    if occurred_at is None or level is None or raw_level is None:
        return None
    exception_type = _text(_field(record, options.get("exceptionTypeField")))
    exception = None if not exception_type else ExceptionInfo(
        exception_type, _text(_field(record, options.get("exceptionMessageField"))) or "")
    return LogEntry(stream, position, occurred_at, _text(raw_time), level, raw_level,
                    _text(_field(record, options.get("categoryField"))),
                    entries.event_id(_field(record, options.get("eventIdField"))),
                    _text(_field(record, options["messageField"])) or "", exception, line)


def parse_time(value: Any, local: ZoneInfo) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / MILLISECONDS_PER_SECOND if value > MILLISECOND_THRESHOLD else value
        try:
            return entries.to_utc(datetime.fromtimestamp(seconds, UTC), local)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return entries.to_utc(datetime.fromisoformat(value), local)
        except ValueError:
            return None
    return None


def _field(record: Mapping[str, Any], path: str | None) -> Any:
    if path is None:
        return _MISSING
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _text(value: Any) -> str | None:
    if value is _MISSING or value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
