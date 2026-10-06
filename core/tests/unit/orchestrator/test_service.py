from datetime import timedelta, timezone

import pytest
from orchestrator_world import FakeModules
from pipeline_world import NOW, make_world, save_issue

from tightrein.domain.enums import IssueStatus, RunStage, RunStatus
from tightrein.domain.run import Run
from tightrein.orchestrator.rules import RunRequest
from tightrein.orchestrator.service import Orchestrator
from tightrein.store import locks
from tightrein.store.locks import FileLockBusy, Holder
from tightrein.store.repos import runs, schedule_state


def orchestrator(world, modules):
    return Orchestrator(modules, layout=world.layout, conn=world.conn, config=world.config, clock=world.clock,
                        events=world.events, notifier=None, zone=timezone.utc, alive=lambda pid: False,
                        host="this-host")


def test_a_run_records_the_loop_run_its_children_and_the_summary(tmp_path):
    world = make_world(tmp_path)
    save_issue(world.conn, "0007", IssueStatus.NEEDS_DECISION)
    runs.save(world.conn, Run("R-20261005-020000-loop", RunStage.LOOP, NOW, RunStatus.RUNNING, holder_pid=4001,
                              holder_host="this-host"))
    locks.acquire(world.conn, "R-20261005-020000-loop", world.clock, timedelta(hours=1),
                  run_id="R-20261005-020000-loop", holder=Holder(4001, "this-host"))

    def lessons():
        runs.save(world.conn, Run("R-20261005-030001-learn", RunStage.LEARN, NOW, RunStatus.OK))

    report = orchestrator(world, FakeModules(world, learn_lessons=lessons)).run(RunRequest())
    assert report.run.status is RunStatus.BLOCKED and report.run.ended_at is not None
    assert runs.get(world.conn, "R-20261005-030001-learn").parent_run_id == report.run.id
    assert runs.get(world.conn, "R-20261005-020000-loop").status is RunStatus.INTERRUPTED
    assert report.outputs["steps"][0]["name"] == "recovery"
    assert report.report == world.layout.daily_report(NOW.date()) and report.report.is_file()
    status = orchestrator(world, FakeModules(world)).status()
    assert status["lastRun"]["runId"] == report.run.id and status["waiting"][0]["subjectId"] == "0007"


def test_a_busy_run_lock_is_skipped_and_recorded_for_scheduled_runs(tmp_path):
    world = make_world(tmp_path)
    with locks.file_lock(world.layout.run_lock(), wait=False):
        with pytest.raises(FileLockBusy):
            orchestrator(world, FakeModules(world)).run(RunRequest(scheduled=True))
    assert schedule_state.get(world.conn, "run").last_status is RunStatus.SKIPPED
    assert runs.find(world.conn, stage=RunStage.LOOP) == []


def test_continue_records_a_loop_run(tmp_path):
    world = make_world(tmp_path)
    save_issue(world.conn, "0007", IssueStatus.NEEDS_DECISION)
    result = orchestrator(world, FakeModules(world)).continue_(["7"])
    assert result.report.stops[0].gate == "issue-approval"
    assert result.run.stage is RunStage.LOOP and result.run.status is RunStatus.BLOCKED


def test_continue_tracks_pull_requests_for_auto_merge(tmp_path):
    """关卡 merge 为 auto：continue 遇到待合并的 Issue 先跟踪 PR(满足条件即自动合并)，不等定时运行。"""
    world = make_world(tmp_path, gates={"merge": "auto"})
    save_issue(world.conn, "0007", IssueStatus.PENDING_MERGE)
    modules = FakeModules(world)
    orchestrator(world, modules).continue_(["7"])
    assert "release.track" in modules.names()
    manual = make_world(tmp_path / "manual")
    save_issue(manual.conn, "0007", IssueStatus.PENDING_MERGE)
    modules = FakeModules(manual)
    orchestrator(manual, modules).continue_(["7"])
    assert "release.track" not in modules.names()
