"""时间：所有需要当前时间的逻辑通过 Clock 获取，存储与交接一律使用 UTC。

按「天」计数的规则(通知去重、每日预算)以本机时区的日期为准，与用户感知的「今天」一致；时区可以注入，测试不依赖本机设置。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)


class FixedClock:
    """测试与 --now 参数使用的固定时钟。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FixedClock 需要带时区的时间")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._value

    def advance(self, delta: timedelta) -> None:
        self._value = self._value + delta


def format_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("format_iso 需要带时区的时间")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError(f"时间缺少时区：{text}")
    return value.astimezone(timezone.utc)


def is_workday(day: date, non_working_days: frozenset[date]) -> bool:
    return day.weekday() < 5 and day not in non_working_days


def workdays_between(start: date, end: date, non_working_days: frozenset[date]) -> int:
    """start 之后(不含 start)到 end(含 end)之间的工作日数。"""
    count = 0
    day = start + timedelta(days=1)
    while day <= end:
        if is_workday(day, non_working_days):
            count += 1
        day += timedelta(days=1)
    return count


def local_date(value: datetime, zone: tzinfo | None = None) -> date:
    """value 在本机时区(zone 为空时)或 zone 中的日期。"""
    if value.tzinfo is None:
        raise ValueError("local_date 需要带时区的时间")
    return (value.astimezone() if zone is None else value.astimezone(zone)).date()
