"""core/json-lines：每行一个 JSON 对象的结构化日志(architecture/10 1.4、3.5)。

字段位置由 options 给出，可以用 `.` 表示嵌套，例如 `log.level`：时间(timeField)、级别(levelField)、
消息(messageField)，以及可选的类别、事件编号、异常类型与异常消息。时间可以是 ISO 8601 字符串，也可以是
Unix 时间戳(秒；大于 10^11 时按毫秒)。不是 JSON 对象、没有可用的时间或级别的行计入 unparsed。
不需要跨片段的状态，state 原样返回。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tightrein.extensions.methods import logs, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
MILLISECOND_THRESHOLD = 10 ** 11
MILLISECONDS_PER_SECOND = 1000
MISSING = object()


def field(record: Mapping[str, Any], path: str | None) -> Any:
    if path is None:
        return MISSING
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return MISSING
        value = value[part]
    return value


def parse_time(value: Any, local: ZoneInfo) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / MILLISECONDS_PER_SECOND if value > MILLISECOND_THRESHOLD else value
        try:
            return logs.to_utc(datetime.fromtimestamp(seconds, timezone.utc), local)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return logs.to_utc(datetime.fromisoformat(value), local)
        except ValueError:
            return None
    return None


def text(value: Any) -> str | None:
    if value is MISSING or value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def parse_line(stream: str, position: int, line: str, options: Mapping[str, Any], levels: Mapping[str, str],
               local: ZoneInfo) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    raw_time = field(record, options["timeField"])
    occurred_at = parse_time(raw_time, local)
    raw_level = text(field(record, options["levelField"]))
    level = None if not raw_level else levels.get(raw_level.lower())
    if occurred_at is None or level is None:
        return None
    exception_type = text(field(record, options["exceptionTypeField"]))
    exception = None if not exception_type else {
        "type": exception_type, "message": text(field(record, options["exceptionMessageField"])) or ""}
    return logs.entry(stream, position, occurred_at, text(raw_time), level, raw_level,
                      text(field(record, options["categoryField"])),
                      logs.event_id(field(record, options["eventIdField"])),
                      text(field(record, options["messageField"])) or "", exception, line)


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    options = request.options
    local = logs.zone(options["timezone"])
    levels = logs.level_map(options["levels"])
    entries, unparsed = [], 0
    for chunk in request.input["chunks"]:
        for position, line in logs.lines(chunk):
            found = parse_line(chunk["stream"], position, line, options, levels, local)
            if found is None:
                unparsed += 1
            else:
                entries.append(found)
    return MethodResult({"entries": entries, "state": dict(request.input["state"] or {}), "unparsed": unparsed})


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
