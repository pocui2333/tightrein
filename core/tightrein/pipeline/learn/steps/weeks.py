"""时间窗与工作日(architecture/08 4.1)，以及定时任务的应执行时刻(architecture/09 3.2 第 1 步)。

- 一周为本机时区(或注入的时区)的周一 00:00 到下周一 00:00，换算为 UTC 后查询；
- 工作日为周一到周五并排除 schedule.nonWorkingDays；
- due_times 是纯函数：给出时间段内各定时任务按 days 与 at 应执行的全部时刻。编排层的到期判断复用它。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import is_workday, local_date, workdays_between

DAYS_PER_WEEK = 7
DAILY = "daily"
WORKDAYS = "workdays"
FIRST_WORKDAY = "firstWorkdayOfWeek"


@dataclass(frozen=True)
class Window:
    """[start, end)，UTC。"""

    start: datetime
    end: datetime

    def contains(self, at: datetime | None) -> bool:
        return at is not None and self.start <= at < self.end

    def shifted(self, weeks: int) -> Window:
        delta = timedelta(days=DAYS_PER_WEEK * weeks)
        return Window(self.start + delta, self.end + delta)


def local_moment(day: date, at: time, zone: tzinfo | None) -> datetime:
    """本机时区(zone 为空时)或 zone 中某日某时刻，换算为 UTC。"""
    moment = datetime.combine(day, at, zone) if zone is not None else datetime.combine(day, at).astimezone()
    return moment.astimezone(timezone.utc)


def week_of(day: date, zone: tzinfo | None) -> Window:
    monday = day - timedelta(days=day.weekday())
    next_monday = monday + timedelta(days=DAYS_PER_WEEK)
    return Window(local_moment(monday, time(), zone), local_moment(next_monday, time(), zone))


def week_start(window: Window, zone: tzinfo | None) -> date:
    return local_date(window.start, zone)


def workdays(start: datetime, end: datetime, config: ProjectConfig, zone: tzinfo | None) -> int:
    """start 所在日之后到 end 所在日(含)之间的工作日数，按本机日期计。"""
    return workdays_between(local_date(start, zone), local_date(end, zone), config.non_working_days())


def first_workday(day: date, non_working: frozenset[date]) -> date | None:
    monday = day - timedelta(days=day.weekday())
    for offset in range(DAYS_PER_WEEK):
        candidate = monday + timedelta(days=offset)
        if is_workday(candidate, non_working):
            return candidate
    return None


def _runs_on(days: str, day: date, non_working: frozenset[date]) -> bool:
    if days == DAILY:
        return True
    if days == WORKDAYS:
        return is_workday(day, non_working)
    return first_workday(day, non_working) == day


@dataclass(frozen=True)
class DueTime:
    task: str
    at: datetime


def due_times(tasks: Iterable[Mapping[str, Any]], non_working: frozenset[date], since: datetime, until: datetime,
              zone: tzinfo | None) -> list[DueTime]:
    """[since, until) 内各任务的应执行时刻，按时刻与任务在清单中的顺序排列。"""
    found: list[tuple[datetime, int, str]] = []
    day, last = local_date(since, zone), local_date(until, zone)
    task_list = list(tasks)
    while day <= last:
        for order, task in enumerate(task_list):
            if not _runs_on(task["days"], day, non_working):
                continue
            for text in task["at"]:
                moment = local_moment(day, time.fromisoformat(text), zone)
                if since <= moment < until:
                    found.append((moment, order, task["name"]))
        day += timedelta(days=1)
    return [DueTime(name, moment) for moment, _, name in sorted(found)]
