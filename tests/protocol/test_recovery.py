import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tightrein.protocol import handoff, recovery
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.naming import FileName, FixedClock, format_iso
from tightrein.protocol.process import Interrupted
from tightrein.protocol.records import EventLog
from tightrein.protocol.recovery import Closed, Mode
from tightrein.protocol.security import Redactor
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import issues, runs
from tightrein.store.tables.issues import Issue
from tightrein.store.tables.runs import Run

NOW = datetime(2026, 10, 7, 9, 30, 0, tzinfo=UTC)
HOST = "this-host"
ME = 4001
DEAD = 4002
ALIVE = 4003
SETTINGS = Settings.from_data({"limits": {"lock": {"stale": "90s"}, "shutdownGrace": "30s"}})


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(tmp_path / "workspaces" / "demo")


@pytest.fixture
def conn(layout: WorkspaceLayout) -> Iterator[sqlite3.Connection]:
    connection = open_database(layout.database)
    yield connection
    connection.close()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def events_for(layout: WorkspaceLayout, clock: FixedClock) -> Callable[[str], EventLog]:
    return lambda run: EventLog(layout.events(run), Redactor(), clock)


def read_events(layout: WorkspaceLayout, run: str) -> list[dict]:
    return [json.loads(line) for line in layout.events(run).read_text(encoding="utf-8").splitlines()]


def running(conn: sqlite3.Connection, run_id: str, *, pid: int, host: str = HOST,
            heartbeat: datetime = NOW) -> None:
    runs.start(conn, Run(run_id, "implement", "schedule", "running", NOW - timedelta(minutes=5),
                         heartbeat_at=heartbeat, holder_pid=pid, holder_host=host))


def status(conn: sqlite3.Connection, run_id: str) -> str:
    run = runs.get(conn, run_id)
    assert run is not None
    return run.status


def step(layout: WorkspaceLayout, point: str, *, at: str, round: int | None = None, commit: str | None = None) -> Path:
    facts: dict = {"files": ["a.py"]}
    if commit is not None:
        facts[recovery.CHECKPOINT_COMMIT] = commit
    path = layout.step_file("0018", FileName(point, "handoff", "json", round=round))
    handoff.write(path, Handoff(point=point, subject="0018", run="R-20261007T093000Z-implement",
                                status=Status.PASSED, summary="完成", facts=facts, round=round, created_at=at))
    return path


# 检查点


def test_the_last_checkpoint_is_the_last_step_written_not_the_last_name(layout: WorkspaceLayout) -> None:
    step(layout, "implement.code", at="2026-10-07T09:00:00Z", round=1, commit="c1")
    step(layout, "implement.review", at="2026-10-07T09:10:00Z", round=1)
    latest = step(layout, "implement.code", at="2026-10-07T09:20:00Z", round=2, commit="c2")
    (latest.parent / "36-implement.check.r2-log.log").write_text("没写完的那一步只留下日志\n", encoding="utf-8")
    found = recovery.last_checkpoint(layout, "0018")
    assert found is not None and found.path == latest and found.round == 2 and found.sequence == 35


def test_steps_finished_in_the_same_second_are_ordered_by_when_the_file_was_written(layout: WorkspaceLayout) -> None:
    # 写入时间只到秒；同一秒内按文件修改时间(纳秒)排，与名字中的序号、轮次无关：
    # 编码第 2 轮(35…r2)在审查第 1 轮(37…r1)之后，不带轮次的交付(38)在第 2 轮审查之后
    written = [("implement.code", 1), ("implement.check", 1), ("implement.review", 1), ("implement.code", 2),
               ("implement.review", 2), ("implement.deliver", None)]
    base = time.time_ns()
    for offset, (point, round_) in enumerate(written):
        path = step(layout, point, at="2026-10-07T09:00:00Z", round=round_)
        os.utime(path, ns=(base + offset * 1000, base + offset * 1000))
    order = [(item.sequence, item.round) for item in recovery.checkpoints(layout, "0018")]
    assert order == [(35, 1), (36, 1), (37, 1), (35, 2), (37, 2), (38, None)]


def test_no_checkpoint_without_a_finished_step(layout: WorkspaceLayout) -> None:
    assert recovery.last_checkpoint(layout, "0018") is None
    assert recovery.checkpoint_commit(layout, "0018") is None


def test_the_worktree_goes_back_to_the_last_recorded_commit(layout: WorkspaceLayout, tmp_path: Path) -> None:
    calls: list[tuple[Path, str]] = []
    worktree = tmp_path / "worktree"

    def reset(path: Path, commit: str) -> None:
        calls.append((path, commit))

    assert recovery.rewind(worktree, layout, "0018", base="b0", reset=reset) == "b0"
    step(layout, "implement.code", at="2026-10-07T09:00:00Z", round=1, commit="c1")
    step(layout, "implement.check", at="2026-10-07T09:05:00Z", round=1)
    assert recovery.rewind(worktree, layout, "0018", base="b0", reset=reset) == "c1"
    assert calls == [(worktree, "b0"), (worktree, "c1")]


# 中断时就地收尾


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (KeyboardInterrupt(), ("interrupted", "命令被中断")),
        (Interrupted(), ("interrupted", "命令被中断")),
        (RuntimeError("x"), ("failed", "命令出错：RuntimeError")),
        (None, ("failed", "命令结束时运行仍为进行中")),
    ],
)
def test_the_closing_status_follows_how_the_command_ended(failure: BaseException | None,
                                                          expected: tuple[str, str]) -> None:
    assert recovery.closing_status(failure) == expected


class FakeLock:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def test_close_own_ends_only_this_process_runs_and_releases_its_locks(
    conn: sqlite3.Connection, layout: WorkspaceLayout, clock: FixedClock
) -> None:
    running(conn, "R-20261007T092500Z-implement", pid=ME)
    running(conn, "R-20261007T092600Z-assess", pid=ALIVE)
    running(conn, "R-20261007T092700Z-collect", pid=ME, host="other-host")
    locks = [FakeLock(), FakeLock()]
    closed = recovery.close_own(conn, clock, pid=ME, host=HOST, failure=KeyboardInterrupt(), locks=locks,
                                events_for=events_for(layout, clock))
    assert closed == [Closed("R-20261007T092500Z-implement", "interrupted", "命令被中断")]
    assert all(lock.released for lock in locks)
    ended = runs.get(conn, "R-20261007T092500Z-implement")
    assert ended is not None and ended.ended_at == NOW
    assert status(conn, "R-20261007T092600Z-assess") == "running"
    assert status(conn, "R-20261007T092700Z-collect") == "running"
    event = read_events(layout, "R-20261007T092500Z-implement")[-1]
    assert (event["kind"], event["point"], event["summary"]) == (
        "decision", "protocol.recovery", "命令被中断，运行标为 interrupted")


def test_runs_left_running_after_an_error_are_marked_failed(
    conn: sqlite3.Connection, layout: WorkspaceLayout, clock: FixedClock
) -> None:
    running(conn, "R-20261007T092500Z-implement", pid=ME)
    recovery.close_own(conn, clock, pid=ME, host=HOST, failure=None, locks=[], events_for=events_for(layout, clock))
    assert status(conn, "R-20261007T092500Z-implement") == "failed"


def test_the_grace_period_forces_an_exit_when_closing_hangs() -> None:
    exits: list[int] = []
    recovery.within_grace(0.05, lambda: time.sleep(0.3), code=130, force_exit=exits.append)
    assert exits == [130]
    exits.clear()
    recovery.within_grace(5, lambda: None, code=130, force_exit=exits.append)
    time.sleep(0.05)
    assert exits == []
    assert recovery.shutdown_grace(SETTINGS) == 30


# 启动时恢复


def test_runs_whose_own_process_died_are_interrupted(
    conn: sqlite3.Connection, layout: WorkspaceLayout, clock: FixedClock
) -> None:
    running(conn, "R-20261007T092500Z-implement", pid=DEAD)
    running(conn, "R-20261007T092600Z-assess", pid=ALIVE)
    running(conn, "R-20261007T092700Z-collect", pid=DEAD, host="other-host")
    found = recovery.recover(conn, clock, SETTINGS, host=HOST, alive=lambda pid: pid != DEAD,
                             events_for=events_for(layout, clock))
    assert found == [Closed("R-20261007T092500Z-implement", "interrupted", "开始它的进程已不在")]
    assert status(conn, "R-20261007T092600Z-assess") == "running"
    assert status(conn, "R-20261007T092700Z-collect") == "running"  # 其他主机的只看心跳
    assert "从检查点接着做" in read_events(layout, "R-20261007T092500Z-implement")[-1]["summary"]


def test_runs_without_a_heartbeat_for_too_long_are_interrupted(
    conn: sqlite3.Connection, layout: WorkspaceLayout, clock: FixedClock
) -> None:
    running(conn, "R-20261007T092500Z-implement", pid=ALIVE, host="other-host", heartbeat=NOW - timedelta(seconds=91))
    running(conn, "R-20261007T092600Z-assess", pid=ALIVE, host="other-host", heartbeat=NOW - timedelta(seconds=90))
    found = recovery.recover(conn, clock, SETTINGS, host=HOST, alive=lambda pid: True,
                             events_for=events_for(layout, clock))
    assert found == [Closed("R-20261007T092500Z-implement", "interrupted", "超过 90s 没有心跳")]
    assert status(conn, "R-20261007T092600Z-assess") == "running"


def test_the_current_run_is_left_alone(conn: sqlite3.Connection, layout: WorkspaceLayout, clock: FixedClock) -> None:
    running(conn, "R-20261007T093000Z-implement", pid=DEAD)
    assert recovery.recover(conn, clock, SETTINGS, host=HOST, alive=lambda pid: False,
                            events_for=events_for(layout, clock), current="R-20261007T093000Z-implement") == []


# 运行控制


def test_pause_and_resume(layout: WorkspaceLayout, clock: FixedClock) -> None:
    assert recovery.control(layout) is None
    recovery.pause(layout, clock, "等额度重置")
    state = recovery.control(layout)
    assert state == recovery.Control(Mode.PAUSED, format_iso(NOW), "等额度重置")
    assert json.loads(layout.control_file.read_text(encoding="utf-8"))["mode"] == "paused"
    assert recovery.resume(layout) == state
    assert recovery.control(layout) is None and recovery.resume(layout) is None


def test_stop_signals_the_processes_of_running_runs_on_this_host(
    conn: sqlite3.Connection, layout: WorkspaceLayout, clock: FixedClock
) -> None:
    running(conn, "R-20261007T092500Z-implement", pid=ALIVE)
    running(conn, "R-20261007T092600Z-assess", pid=DEAD, host="other-host")
    signalled: list[int] = []
    stopped = recovery.stop(layout, conn, clock, host=HOST, terminate=signalled.append)
    assert stopped == ["R-20261007T092500Z-implement"] and signalled == [ALIVE]
    state = recovery.control(layout)
    assert state is not None and state.mode is Mode.STOPPED


def test_take_and_give_an_issue(conn: sqlite3.Connection, clock: FixedClock) -> None:
    issues.save(conn, Issue("0018", "implementing", "订单越权", "bug", "problem"), clock)
    assert recovery.take(conn, clock, "0018", by="chao").held_by == "chao"
    taken = issues.get(conn, "0018")
    assert taken is not None and taken.held_by == "chao"
    recovery.give(conn, clock, "0018")
    given = issues.get(conn, "0018")
    assert given is not None and given.held_by is None


def test_only_existing_issues_can_be_taken(conn: sqlite3.Connection, clock: FixedClock) -> None:
    with pytest.raises(ValueError, match="只能接管或交还 Issue"):
        recovery.take(conn, clock, "P-0003", by="chao")
    with pytest.raises(LookupError):
        recovery.take(conn, clock, "0099", by="chao")
