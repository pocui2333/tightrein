"""regex：按正则的命名分组从每条日志的首行抽取时间、级别、类别与消息。

- pattern 从行首匹配，必须有命名分组 time、level、message，可选 category 与 eventId；不以它开头的行在 multiline
  为真时并入上一条的原文(堆栈等续行)，片段开头没有上一条可并时计入 unparsed；
- 时间按 timeFormat(strptime 写法)解析，未给出时按 ISO 8601；不带时区的按 timezone 解释；
- timeFormat 只有时刻(不含 %Y、%y、%m、%d、%j、%b、%B)时补日期：按流记住上一条的本地日期时间，比上一条早超过
  rolloverToleranceMinutes 就算跨过午夜(日期加一)；没有状态时以片段最晚时间(为空时取现在)的本地日期为起点，
  比它晚超过容差的算前一天。状态为「流名 → 最后一条的本地日期时间」，跨片段、跨运行传递；
- 时间或级别认不出的行计入 unparsed。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from tightrein.collect.common.source import SourceMisconfigured
from tightrein.collect.platform_errors.log_parse import entries
from tightrein.collect.platform_errors.log_parse.entries import LogEntry, ParseResult
from tightrein.collect.platform_errors.log_platform.chunks import Chunk

REQUIRED_GROUPS = ("time", "level", "message")
DATE_DIRECTIVES = ("%Y", "%y", "%m", "%d", "%j", "%b", "%B")
LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"


class Clock:
    """把原文中的时间换成 UTC；只有时刻的写法按流补日期。"""

    def __init__(self, options: Mapping[str, Any], state: Mapping[str, Any] | None, now: datetime) -> None:
        self.format: str | None = options["timeFormat"]
        self.local = entries.zone(options["timezone"])
        self.tolerance = timedelta(minutes=options["rolloverToleranceMinutes"])
        self.time_only = self.format is not None and not any(item in self.format for item in DATE_DIRECTIVES)
        self.now = now
        self.last: dict[str, datetime] = {}
        for stream, value in (state or {}).items():
            try:
                self.last[stream] = datetime.strptime(value, LOCAL_FORMAT)  # noqa: DTZ007 - 本地墙上时间，补日期用
            except (TypeError, ValueError):
                continue

    def parse(self, text: str, chunk: Chunk) -> datetime | None:
        # 原文多半不带时区：先按原样解析，to_utc 再按配置的时区解释
        try:
            if self.format is None:
                value = datetime.fromisoformat(text)
            else:
                value = datetime.strptime(text, self.format)  # noqa: DTZ007
        except ValueError:
            return None
        if self.time_only:
            value = self.complete(chunk, value)
        return entries.to_utc(value, self.local)

    def complete(self, chunk: Chunk, moment: datetime) -> datetime:
        last = self.last.get(chunk.stream)
        if last is not None:
            value = datetime.combine(last.date(), moment.time())
            if value < last - self.tolerance:
                value += timedelta(days=1)
        else:
            anchor = (chunk.modified_at or self.now).astimezone(self.local).replace(tzinfo=None)
            value = datetime.combine(anchor.date(), moment.time())
            if value > anchor + self.tolerance:
                value -= timedelta(days=1)
        self.last[chunk.stream] = value
        return value

    def state(self) -> dict[str, str]:
        return {stream: value.strftime(LOCAL_FORMAT) for stream, value in sorted(self.last.items())}


def parse(chunks: Sequence[Chunk], options: Mapping[str, Any], state: Mapping[str, Any] | None,
          now: datetime) -> ParseResult:
    pattern = compile_pattern(options["pattern"])
    levels = entries.level_map(options["levels"])
    clock = Clock(options, state, now)
    found: list[LogEntry] = []
    unparsed = 0
    for chunk in chunks:
        current: LogEntry | None = None
        for position, line in entries.lines(chunk):
            match = pattern.match(line)
            if match is None:
                if options["multiline"] and current is not None:
                    current = replace(current, raw=current.raw + "\n" + line)
                    found[-1] = current
                else:
                    unparsed += 1
                continue
            groups = match.groupdict()
            raw_level = groups["level"] or ""
            level = levels.get(raw_level.lower())
            occurred_at = None if level is None else clock.parse(groups["time"] or "", chunk)
            if level is None or occurred_at is None:
                unparsed += 1
                current = None
                continue
            current = LogEntry(chunk.stream, position, occurred_at, groups["time"], level, raw_level,
                               groups.get("category"), entries.event_id(groups.get("eventId")),
                               groups["message"] or "", None, line)
            found.append(current)
    return ParseResult(found, clock.state() if clock.time_only else dict(state or {}), unparsed)


def compile_pattern(text: str) -> re.Pattern[str]:
    try:
        pattern = re.compile(text)
    except re.error as error:
        raise SourceMisconfigured(f"regex 的 pattern 不是合法的正则：{error}") from error
    missing = [name for name in REQUIRED_GROUPS if name not in pattern.groupindex]
    if missing:
        raise SourceMisconfigured(f"regex 的 pattern 缺少命名分组：{', '.join(missing)}")
    return pattern
