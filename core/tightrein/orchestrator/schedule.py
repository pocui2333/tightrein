"""时间表与到期判断(architecture/09 3.2)。

- 应执行时刻由 pipeline/learn/steps/weeks.due_times 计算(learn 的漏跑检查用同一个函数)；
- 任务的 last_started_at 早于最近一次应执行时刻即到期；错过多个时刻只补执行一次，错过的次数记入 missed_count；
  从未执行过的任务向前看 loop.lookbackDays 天，不计错过次数；
- 周任务(learn report)的时刻为 schedule.weekly.at，日期为本周第一个工作日，以任务名 weekly 记录；
- 运行锁被占用时以任务名 run 记一条 skipped；
- 完整运行的时刻为 schedule.runAt(工作日)，以任务名 daily-run 记录；launchd 每次唤醒 tick，到期时做完整运行，
  其余只做事件运行(redesign/09-loop.md 第 5 节)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import RunStatus
from tightrein.pipeline.learn.steps import weeks
from tightrein.store.repos import schedule_state
from tightrein.store.repos.schedule_state import ScheduleState

WEEKLY = "weekly"
WEEKLY_COMMAND = "learn report"
RUN_TASK = "run"
DAILY_RUN = "daily-run"
DAILY_COMMAND = "run --scheduled"
INCLUSIVE = timedelta(microseconds=1)


@dataclass(frozen=True)
class DueTask:
    name: str
    command: str
    due_at: datetime
    missed: int


def _due(task: Mapping[str, Any], config: ProjectConfig, conn: sqlite3.Connection, now: datetime,
         zone: tzinfo | None) -> DueTask | None:
    state = schedule_state.get(conn, task["name"])
    last = state.last_started_at if state is not None else None
    since = last + INCLUSIVE if last is not None else now - timedelta(days=int(config.get("loop.lookbackDays")))
    found = weeks.due_times([task], config.non_working_days(), since, now + INCLUSIVE, zone)
    if not found:
        return None
    return DueTask(task["name"], task["command"], found[-1].at, len(found) - 1 if last is not None else 0)


def due_tasks(config: ProjectConfig, conn: sqlite3.Connection, now: datetime, zone: tzinfo | None) -> list[DueTask]:
    """schedule.tasks 中到期的任务，按书写顺序。"""
    found = [_due(task, config, conn, now, zone) for task in config.data.get("schedule", {}).get("tasks", [])]
    return [task for task in found if task is not None]


def weekly_due(config: ProjectConfig, conn: sqlite3.Connection, now: datetime, zone: tzinfo | None) -> DueTask | None:
    task = {"name": WEEKLY, "days": weeks.FIRST_WORKDAY, "at": [config.get("schedule.weekly.at")],
            "command": WEEKLY_COMMAND}
    return _due(task, config, conn, now, zone)


def run_due(config: ProjectConfig, conn: sqlite3.Connection, now: datetime, zone: tzinfo | None) -> DueTask | None:
    """工作日 schedule.runAt 的完整运行是否到期。"""
    task = {"name": DAILY_RUN, "days": "workdays", "at": list(config.get("schedule.runAt")), "command": DAILY_COMMAND}
    return _due(task, config, conn, now, zone)


def started(conn: sqlite3.Connection, task: str, at: datetime, run_id: str, missed: int = 0) -> None:
    previous = schedule_state.get(conn, task) or ScheduleState(task)
    schedule_state.save(conn, replace(previous, missed_count=missed, last_started_at=at, last_ended_at=None,
                                      last_status=RunStatus.RUNNING, last_run_id=run_id))


def ended(conn: sqlite3.Connection, task: str, at: datetime, status: RunStatus) -> None:
    previous = schedule_state.get(conn, task) or ScheduleState(task)
    schedule_state.save(conn, replace(previous, last_ended_at=at, last_status=status))


def skipped(conn: sqlite3.Connection, at: datetime) -> None:
    """定时运行因上一次运行尚未结束而跳过。"""
    previous = schedule_state.get(conn, RUN_TASK) or ScheduleState(RUN_TASK)
    schedule_state.save(conn, replace(previous, last_started_at=at, last_ended_at=at, last_status=RunStatus.SKIPPED))
