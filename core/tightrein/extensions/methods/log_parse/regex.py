"""core/regex：按正则抽取时间、级别、类别与消息(architecture/10 1.4、3.5)。

- options.pattern 从行首匹配，必须有命名分组 time、level、message，可选 category 与 eventId；不以它开头的行在
  multiline 为真时并入上一条(堆栈等续行，只进入 raw)，片段开头没有上一条可并时计入 unparsed。
- 时间按 options.timeFormat(strptime 写法)解析，未给出时按 ISO 8601；不带时区的时间按 options.timezone 解释。
- timeFormat 只有时刻(不含 %Y、%y、%m、%d、%j、%b、%B)时补齐日期：从 state 中该流上一条的本地日期时间顺序推进，
  比上一条早超过 rolloverToleranceMinutes 视为跨过午夜，日期加一；没有 state 时以片段的 modifiedAt(换算到本地时区，
  为空时取当前时间)的日期为起点，时刻比它晚超过容差的算作前一天。state 为「流名 → 最后一条的本地日期时间」。
- 时间或级别无法识别的行计入 unparsed。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import logs, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
REQUIRED_GROUPS = ("time", "level", "message")
DATE_DIRECTIVES = ("%Y", "%y", "%m", "%d", "%j", "%b", "%B")
LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"


def compile_pattern(text: str) -> re.Pattern[str]:
    try:
        pattern = re.compile(text)
    except re.error as error:
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"options.pattern 不是合法的正则：{error}") from error
    missing = [name for name in REQUIRED_GROUPS if name not in pattern.groupindex]
    if missing:
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"options.pattern 缺少命名分组：{', '.join(missing)}")
    return pattern


class Clock:
    """把原文中的时间换算为 UTC；只有时刻的写法按流补齐日期。"""

    def __init__(self, options: Mapping[str, Any], state: Mapping[str, Any] | None, now: datetime) -> None:
        self.format = options["timeFormat"]
        self.local = logs.zone(options["timezone"])
        self.tolerance = timedelta(minutes=options["rolloverToleranceMinutes"])
        self.time_only = self.format is not None and not any(item in self.format for item in DATE_DIRECTIVES)
        self.now = now
        self.last: dict[str, datetime] = {}
        for stream, value in (state or {}).items():
            try:
                self.last[stream] = datetime.strptime(value, LOCAL_FORMAT)
            except (TypeError, ValueError):
                continue

    def parse(self, text: str, chunk: Mapping[str, Any]) -> str | None:
        try:
            value = datetime.fromisoformat(text) if self.format is None else datetime.strptime(text, self.format)
        except ValueError:
            return None
        if self.time_only:
            value = self.complete(chunk, value)
        return logs.to_utc(value, self.local)

    def complete(self, chunk: Mapping[str, Any], moment: datetime) -> datetime:
        stream = chunk["stream"]
        last = self.last.get(stream)
        if last is not None:
            value = datetime.combine(last.date(), moment.time())
            if value < last - self.tolerance:
                value += timedelta(days=1)
        else:
            modified = chunk.get("modifiedAt")
            anchor = parse_iso(modified) if modified else self.now
            local_anchor = anchor.astimezone(self.local).replace(tzinfo=None)
            value = datetime.combine(local_anchor.date(), moment.time())
            if value > local_anchor + self.tolerance:
                value -= timedelta(days=1)
        self.last[stream] = value
        return value

    def state(self) -> dict[str, str]:
        return {stream: value.strftime(LOCAL_FORMAT) for stream, value in sorted(self.last.items())}


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    options = request.options
    pattern = compile_pattern(options["pattern"])
    levels = logs.level_map(options["levels"])
    clock = Clock(options, request.input["state"], context.now().astimezone(timezone.utc))
    entries: list[dict[str, Any]] = []
    unparsed = 0
    for chunk in request.input["chunks"]:
        current: dict[str, Any] | None = None
        for position, line in logs.lines(chunk):
            match = pattern.match(line)
            if match is None:
                if options["multiline"] and current is not None:
                    current["raw"] += "\n" + line
                else:
                    unparsed += 1
                continue
            groups = match.groupdict()
            raw_level = groups["level"] or ""
            level = levels.get(raw_level.lower())
            occurred_at = None if level is None else clock.parse(groups["time"] or "", chunk)
            if occurred_at is None:
                unparsed += 1
                current = None
                continue
            current = logs.entry(chunk["stream"], position, occurred_at, groups["time"], level, raw_level,
                                 groups.get("category"), logs.event_id(groups.get("eventId")),
                                 groups["message"] or "", None, line)
            entries.append(current)
    state = clock.state() if clock.time_only else dict(request.input["state"] or {})
    return MethodResult({"entries": entries, "state": state, "unparsed": unparsed})


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
