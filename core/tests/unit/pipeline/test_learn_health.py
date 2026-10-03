from datetime import datetime, timedelta, timezone

from learn_world import ZONE, make_learn_world, save_run, save_signal
from pipeline_world import NOW

from tightrein.domain.enums import (
    DeploymentStatus,
    Probe,
    ProbeLevel,
    RunStage,
    RunStatus,
)
from tightrein.pipeline.learn.steps import health
from tightrein.pipeline.learn.steps.health import HealthContext
from tightrein.store import locks
from tightrein.store.locks import ObjectLock
from tightrein.store.repos import deployments
from tightrein.store.repos.deployments import Deployment

SCHEDULE = {"tick": {"weekdays": [1, 2, 3, 4, 5], "minutes": [0]},
            "tasks": [{"name": "static", "days": "workdays", "at": ["08:30"], "command": "collect --probe static"},
                      {"name": "issues", "days": "workdays", "at": ["09:00"], "command": "issue sync"},
                      {"name": "custom", "days": "workdays", "at": ["09:00"], "command": "echo hello"}]}


def context(world, now=NOW, alive=lambda pid: False, current=None):
    return HealthContext(world.conn, world.layout, world.config, now, ZONE, current, alive)


def failing(items):
    return [(item.check, item.notify) for item in items if not item.passed]


def static_run(world, at):
    save_run(world, f"R-{at:%Y%m%d-%H%M%S}-collect-static", probe=Probe.STATIC, started=at)


def test_missed_runs_notify_only_when_the_latest_ones_are_missed(tmp_path):
    world = make_learn_world(tmp_path, schedule=SCHEDULE)
    now = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)  # 周三 12:00(UTC+9)
    for month, day in ((9, 30), (10, 1), (10, 5)):  # 周四、周五、周二 08:30 之后
        static_run(world, datetime(2026, month, day, 23, 35, tzinfo=timezone.utc))
    for day in (1, 2, 5, 6, 7):
        save_run(world, f"R-202610{day:02d}-000500-issue", RunStage.ISSUE,
                 started=datetime(2026, 10, day, 0, 5, tzinfo=timezone.utc))
    items = health.missed_runs(context(world, now))
    assert failing(items) == [("missed-runs", False)]
    assert items[0].detail.startswith("定时任务 static 漏跑 2 次：2026-10-05 08:30 +0900、2026-10-07 08:30 +0900")
    later = health.missed_runs(context(world, now + timedelta(days=1)))
    assert failing(later) == [("missed-runs", True), ("missed-runs", False)]


def test_a_new_workspace_counts_missed_runs_from_its_creation(tmp_path):
    world = make_learn_world(tmp_path, schedule=SCHEDULE)
    items = health.missed_runs(context(world, NOW + timedelta(days=2)))
    assert [item.detail.split("：")[0] for item in items] == ["定时任务 static 漏跑 2 次", "定时任务 issues 漏跑 2 次"]


def test_missed_runs_pass_without_schedule(tmp_path):
    world = make_learn_world(tmp_path)
    assert failing(health.missed_runs(context(world))) == []


def test_abnormal_exits_include_failed_runs_and_dead_holders(tmp_path):
    world = make_learn_world(tmp_path)
    save_run(world, "R-20261005-010000-triage", RunStage.TRIAGE, status=RunStatus.FAILED,
             started=NOW - timedelta(hours=2), ended=NOW - timedelta(hours=1))
    save_run(world, "R-20261005-020000-fix", RunStage.FIX, status=RunStatus.RUNNING, started=NOW - timedelta(hours=1))
    save_run(world, "R-20261005-021000-verify", RunStage.VERIFY, status=RunStatus.RUNNING,
             started=NOW - timedelta(minutes=50))
    save_run(world, "R-20261005-030000-learn", RunStage.LEARN, status=RunStatus.RUNNING, started=NOW)
    save_run(world, "R-20260901-010000-fix", RunStage.FIX, status=RunStatus.FAILED, started=NOW - timedelta(days=30))
    locks.TABLE.save(world.conn, ObjectLock("0007", 4242, "host", NOW, NOW + timedelta(hours=1),
                                            "R-20261005-021000-verify"))
    items = health.abnormal_exits(context(world, alive=lambda pid: pid == 4242, current="R-20261005-030000-learn"))
    details = [item.detail for item in items if not item.passed]
    assert len(details) == 2
    assert details[0].startswith("运行 R-20261005-010000-triage 以失败结束；日志 data/logs/events-2026-10-05.jsonl")
    assert details[1].startswith("运行 R-20261005-020000-fix 没有结束时间")
    assert all(item.notify for item in items)


def test_idle_probes_need_the_full_window_without_signals(tmp_path):
    world = make_learn_world(tmp_path)
    for number in range(10):
        save_run(world, f"R-2026100{number % 5}-0{number}0000-collect-alerts", probe=Probe.ALERTS,
                 started=NOW - timedelta(hours=10 - number))
    items = health.idle_probes(context(world))
    assert failing(items) == [("idle-probe", False)]
    assert "业务告警 最近 10 次运行" in items[0].detail
    save_signal(world, 1, "R-20261004-090000-collect-alerts", probe=Probe.ALERTS)
    assert failing(health.idle_probes(context(world))) == []


def test_deploy_stall_waits_for_the_threshold(tmp_path):
    world = make_learn_world(tmp_path)
    deployments.save(world.conn, Deployment("d" * 40, DeploymentStatus.SUCCEEDED, NOW - timedelta(hours=3)))
    deployments.save(world.conn, Deployment("e" * 40, DeploymentStatus.SUCCEEDED, NOW - timedelta(minutes=30)))
    deployments.save(world.conn, Deployment("f" * 40, DeploymentStatus.SUCCEEDED, NOW - timedelta(hours=4)))
    save_run(world, "R-20261005-000000-collect-api-fuzz", probe=Probe.API_FUZZ, commit="f" * 40,
             level=ProbeLevel.SHALLOW)
    items = health.deploy_stall(context(world))
    assert failing(items) == [("deploy-stall", True)]
    assert "dddddddddddd" in items[0].detail


def test_account_unavailable_reads_the_latest_collect_run(tmp_path):
    world = make_learn_world(tmp_path)
    save_run(world, "R-20261005-010000-collect-api-fuzz", probe=Probe.API_FUZZ, started=NOW - timedelta(hours=2),
             failed_roles=("Viewer",))
    save_run(world, "R-20261005-020000-collect-api-fuzz", probe=Probe.API_FUZZ, started=NOW - timedelta(hours=1))
    save_run(world, "R-20261005-023000-collect-static", probe=Probe.STATIC, started=NOW - timedelta(minutes=30))
    assert failing(health.account_unavailable(context(world))) == []
    save_run(world, "R-20261005-025000-collect-api-fuzz", probe=Probe.API_FUZZ, started=NOW - timedelta(minutes=10),
             failed_roles=("Viewer",))
    items = health.account_unavailable(context(world))
    assert failing(items) == [("account-unavailable", True)]
    assert "Viewer" in items[0].detail and "R-20261005-025000-collect-api-fuzz" in items[0].detail


def test_data_dir_size_lists_subdirectories_over_the_limit(tmp_path):
    world = make_learn_world(tmp_path, thresholds={
        "suppressionDays": {"value": 30, "min": 7, "max": 90},
        "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}},
        "health": {"dataDirMaxBytes": {"value": 104857600, "min": 104857600, "max": 1099511627776}}})
    assert failing(health.data_dir_size(context(world))) == []
    big = world.layout.data_dir() / "runs" / "big.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    with open(big, "wb") as handle:
        handle.truncate(104857600 + 1)
    items = health.data_dir_size(context(world))
    assert failing(items) == [("data-dir-size", False)]
    assert "runs 104857601 字节" in items[0].detail


def test_check_runs_all_six(tmp_path):
    world = make_learn_world(tmp_path)
    assert [item.check for item in health.check(context(world))] == [
        "missed-runs", "abnormal-exit", "idle-probe", "deploy-stall", "account-unavailable", "data-dir-size"]
