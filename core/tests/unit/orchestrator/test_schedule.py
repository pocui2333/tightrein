from datetime import datetime, timedelta, timezone

import pytest

from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import RunStatus
from tightrein.orchestrator import schedule
from tightrein.store.migrations.runner import open_database
from tightrein.store.repos import schedule_state

JST = timezone(timedelta(hours=9))
TICK = {"weekdays": [1, 2, 3, 4, 5], "minutes": [0, 30]}
STATIC = {"name": "static", "days": "workdays", "at": ["08:30", "13:30"], "command": "collect --probe static"}


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=JST)


@pytest.fixture
def conn(tmp_path):
    connection = open_database(tmp_path / "db.sqlite", FixedClock(at(5, 0)))
    yield connection
    connection.close()


def config_with(make_config, *tasks, **extra):
    return make_config(schedule={"tick": TICK, "tasks": list(tasks), **extra})


def test_a_task_never_run_is_due_at_its_latest_time_without_misses(make_config, conn):
    config = config_with(make_config, STATIC)
    assert schedule.due_tasks(config, conn, at(5, 9), JST) == [
        schedule.DueTask("static", "collect --probe static", at(5, 8, 30), 0)]
    assert schedule.due_tasks(config, conn, at(5, 8, 29), JST)[0].due_at == at(2, 13, 30)


def test_a_started_task_waits_for_the_next_time(make_config, conn):
    config = config_with(make_config, STATIC)
    schedule.started(conn, "static", at(5, 8, 30) + timedelta(seconds=10), "R-20261004-233010-loop")
    assert schedule.due_tasks(config, conn, at(5, 13, 29), JST) == []
    assert schedule.due_tasks(config, conn, at(5, 13, 30), JST)[0].due_at == at(5, 13, 30)


def test_missed_times_run_once_and_are_counted(make_config, conn):
    config = config_with(make_config, STATIC)
    schedule.started(conn, "static", at(2, 13, 30), "R-20261002-043000-loop")
    due = schedule.due_tasks(config, conn, at(5, 14), JST)
    assert [(task.due_at, task.missed) for task in due] == [(at(5, 13, 30), 1)]


def test_non_working_days_and_the_first_workday_of_the_week(make_config, conn):
    weekly_task = {"name": "full", "days": "firstWorkdayOfWeek", "at": ["08:30"], "command": "collect --probe static"}
    config = config_with(make_config, weekly_task, nonWorkingDays=["2026-10-05"])
    assert schedule.due_tasks(config, conn, at(5, 12), JST) == []
    assert schedule.due_tasks(config, conn, at(6, 9), JST)[0].due_at == at(6, 8, 30)
    assert schedule.weekly_due(config, conn, at(6, 9), JST).due_at == at(6, 8, 30)
    assert schedule.weekly_due(config, conn, at(6, 8), JST) is None


def test_daily_tasks_across_midnight(make_config, conn):
    nightly = {"name": "deep", "days": "daily", "at": ["23:30"], "command": "collect --probe api-fuzz --level deep"}
    config = config_with(make_config, nightly)
    schedule.started(conn, "deep", at(4, 23, 30), "R-20261004-143000-loop")
    assert schedule.due_tasks(config, conn, at(5, 0, 10), JST) == []
    assert schedule.due_tasks(config, conn, at(5, 23, 31), JST)[0].due_at == at(5, 23, 30)


def test_state_records_start_end_and_skips(conn):
    schedule.started(conn, "static", at(5, 8, 30), "R-20261004-233000-loop", missed=2)
    schedule.ended(conn, "static", at(5, 8, 40), RunStatus.OK)
    state = schedule_state.get(conn, "static")
    assert (state.missed_count, state.last_status, state.last_ended_at, state.last_run_id) == (
        2, RunStatus.OK, at(5, 8, 40), "R-20261004-233000-loop")
    schedule.skipped(conn, at(5, 9))
    assert schedule_state.get(conn, "run").last_status is RunStatus.SKIPPED
