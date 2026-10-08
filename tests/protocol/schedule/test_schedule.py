import importlib
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.assess.issue import transitions
from tightrein.onboard.check import READY_KEY
from tightrein.protocol import recovery, schedule
from tightrein.protocol.git import worktrees
from tightrein.protocol.naming import format_iso
from tightrein.protocol.schedule import RunStatus, StepStatus
from tightrein.settings.load import Settings
from tightrein.store.tables import counters, issues, problems, runs, state

for package in ("tightrein.collect", "tightrein.implement", "tightrein.release", "tightrein.retro"):
    importlib.import_module(package)  # conftest 替换各阶段入口时要在包上设属性：包先导入

HOST = "test-host"
NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)  # 与 conftest 的时钟相同


def run(runtime: Any, **options: Any) -> schedule.Outcome:
    options.setdefault("trigger", "manual")
    return schedule.run(runtime, host=HOST, alive=lambda pid: True, **options)


def add_issue(runtime: Any, issue_id: str, status: str, severity: str = "P2", held_by: str | None = None) -> None:
    issues.save(runtime.conn, issues.Issue(id=issue_id, status=status, title=f"Issue {issue_id}", kind="bug",
                                           origin="problem", severity=severity, held_by=held_by), runtime.clock)


def add_problem(runtime: Any, problem_id: str, status: str = "new") -> None:
    problems.save(runtime.conn, problems.Problem(id=problem_id, fingerprint=problem_id, source="collect.alerts",
                                                 check_type="alert", status=status, title="出错",
                                                 first_seen=NOW, last_seen=NOW), runtime.clock)


def ready(runtime: Any) -> None:
    state.put(runtime.conn, READY_KEY, format_iso(NOW), runtime.clock)


def window(kit: Any, start: str, end: str, days: list[str]) -> Settings:
    return Settings.from_data(kit.DEFAULTS, {"schedule": {"window": {"from": start, "to": end, "days": days}}})


# 按状态推进


def test_a_manual_run_goes_through_every_stage_in_order_and_ends_with_retro(stages, make_runtime):
    runtime = make_runtime()
    outcome = run(runtime)
    assert outcome.status is RunStatus.DONE
    assert stages.names() == ["collect", "assess_pending", "queue", "retro"]
    assert stages.calls[0] == ("collect", ["collect.alerts"])  # 手动触发：启用的来源全部交给采集，不看是否到点
    recorded = runs.get(runtime.conn, runtime.run)
    assert (recorded.status, recorded.trigger, recorded.stage) == ("done", "manual", "run")
    assert [step["stage"] for step in recorded.summary["steps"]] == ["collect", "retro"]


def test_a_scheduled_run_leaves_the_choice_of_sources_to_collect_and_stops_at_advance_to(stages, make_runtime):
    runtime = make_runtime({"schedule": {"advanceTo": "assess"}})
    ready(runtime)
    outcome = run(runtime, trigger="schedule")
    assert outcome.status is RunStatus.DONE
    assert stages.names() == ["collect", "assess_pending", "retro"]
    assert stages.calls[0] == ("collect", None)


def test_a_failing_step_is_recorded_and_later_steps_still_run(stages, make_runtime):
    runtime = make_runtime()

    def broken(runtime: Any, only: Any) -> Any:
        raise RuntimeError("平台不通")

    stages.collect = broken
    outcome = run(runtime)
    assert outcome.status is RunStatus.FAILED
    assert outcome.steps[0] == schedule.Step("collect", None, StepStatus.FAILED, "RuntimeError: 平台不通")
    assert stages.names()[-1] == "retro"
    assert runs.get(runtime.conn, runtime.run).status == "failed"


def test_a_failed_assessment_counts_for_the_object_and_the_others_go_on(stages, make_runtime, kit):
    runtime = make_runtime()
    stages.assess_pending = lambda runtime: [kit.assessed("P-0001", "failed"), kit.assessed("P-0002")]
    outcome = run(runtime, stage="assess")
    assert [(step.subject, step.status) for step in outcome.steps] == [("P-0001", StepStatus.FAILED),
                                                                      ("P-0002", StepStatus.PASSED)]
    assert counters.get(runtime.conn, "breaker.object.P-0001") == 1


def test_a_given_problem_is_only_assessed(stages, make_runtime):
    runtime = make_runtime()
    add_problem(runtime, "P-0003")
    run(runtime, subject="P-0003")
    assert stages.calls == [("assess", "P-0003")]


def test_an_unknown_subject_is_refused_before_anything_starts(stages, make_runtime):
    runtime = make_runtime()
    with pytest.raises(LookupError):
        run(runtime, subject="0042")
    with pytest.raises(ValueError):
        run(runtime, stage="deploy")
    assert stages.calls == [] and runs.latest(runtime.conn) is None


# 实施：同时只处理一个 Issue


def test_one_issue_at_a_time_the_running_one_first_then_by_severity(stages, make_runtime):
    runtime = make_runtime()
    add_issue(runtime, "0004", "todo", "P2")
    add_issue(runtime, "0005", "todo", "P1")
    add_issue(runtime, "0006", "implementing", "P3")
    add_issue(runtime, "0007", "implementing", "P0", held_by="alice")  # 手动接管的不碰
    run(runtime, stage="implement")
    assert stages.calls == [("implement", "0006")]
    issues.save(runtime.conn, issues.Issue(id="0006", status="done", title="x", kind="bug", origin="problem"),
                runtime.clock)
    run(make_runtime(run="R-20261008T030001Z-implement"), stage="implement")
    assert stages.calls[-1] == ("implement", "0005")


def test_an_issue_is_driven_step_by_step_until_a_gate(stages, make_runtime, kit):
    runtime = make_runtime()
    add_issue(runtime, "0006", "implementing")
    answers = iter([kit.stepped("0006", "implement.locate", "implement.design"),
                    kit.stepped("0006", "implement.design", "implement.approve"),
                    kit.stepped("0006", "implement.approve", None, "pending")])
    stages.implement = lambda runtime, issue: next(answers)
    outcome = run(runtime, stage="implement")
    assert stages.names() == ["implement"] * 3
    assert outcome.pending and not outcome.failed


def test_a_step_that_does_not_move_counts_as_a_failure(stages, make_runtime, kit):
    runtime = make_runtime()
    add_issue(runtime, "0006", "implementing")
    stages.implement = lambda runtime, issue: kit.stepped(issue, "implement.code", "implement.check")
    run(runtime, stage="implement")
    assert stages.names() == ["implement", "implement"]
    assert counters.get(runtime.conn, "breaker.object.0006") == 1


def test_the_breaker_trips_after_repeated_failures_and_resets_on_progress(stages, make_runtime, monkeypatch, kit):
    taken: list[tuple[str, Any, str]] = []
    monkeypatch.setattr(transitions, "apply_event", lambda runtime, subject, event, **options: taken.append(
        (subject, event, options["actor"])))
    first = make_runtime()
    add_issue(first, "0006", "implementing")

    def broken(runtime: Any, issue: str) -> Any:
        raise RuntimeError("编码失败")

    stages.implement = broken
    for number in range(2):
        run(make_runtime(run=f"R-20261008T03000{number}Z-implement"), stage="implement")
    assert counters.get(first.conn, "breaker.object.0006") == 2 and taken == []
    stages.implement = lambda runtime, issue: kit.stepped(issue, "implement.code", None)
    run(make_runtime(run="R-20261008T030002Z-implement"), stage="implement")
    assert counters.get(first.conn, "breaker.object.0006") == 0  # 前进一步即清零
    stages.implement = broken
    for number in range(3, 6):
        run(make_runtime(run=f"R-20261008T03000{number}Z-implement"), stage="implement")
    assert taken == [("0006", transitions.IssueEvent.TAKE, schedule.BREAKER_HOLDER)]
    assert counters.get(first.conn, "breaker.object.0006") == 0  # 熔断后清零


def test_an_open_breaker_skips_the_object(stages, make_runtime):
    runtime = make_runtime()
    add_issue(runtime, "0006", "implementing")
    counters.add(runtime.conn, "breaker.object.0006", 3, runtime.clock)
    outcome = run(runtime, stage="implement")
    assert stages.calls == []
    assert outcome.steps == [schedule.Step("implement", "0006", StepStatus.SKIPPED, "对象熔断已打开")]


def test_at_the_quota_reserve_no_new_object_is_started_but_the_running_one_goes_on(stages, make_runtime):
    runtime = make_runtime()
    runtime.agents.quota = SimpleNamespace(reserve_reached=lambda: True, reserve_reasons=lambda: ["5h 已用 92%"],
                                           halted_until=lambda: None)
    add_issue(runtime, "0004", "todo")
    outcome = run(runtime, stage="implement")
    assert stages.calls == [] and outcome.steps[0].status is StepStatus.SKIPPED
    assert "5h 已用 92%" in outcome.steps[0].summary
    add_issue(runtime, "0006", "implementing")
    second = make_runtime(run="R-20261008T030001Z-run")
    second.agents.quota = runtime.agents.quota
    run(second)
    assert ("implement", "0006") in stages.calls and "assess_pending" not in stages.names()


# 发布：发布中的只交给合并队列，验收中的与指定的才单独推进


def test_releasing_issues_only_go_through_the_queue_and_accepting_ones_go_one_by_one(stages, make_runtime, kit):
    runtime = make_runtime()
    add_issue(runtime, "0001", "releasing")
    add_issue(runtime, "0002", "accepting")
    add_issue(runtime, "0003", "accepting", held_by="alice")
    stages.queue = lambda runtime: [kit.stepped("0001", "release.ci", None, "pending")]
    outcome = run(runtime, stage="release")
    assert stages.calls == [("queue", None), ("release", "0002")]
    assert [(step.subject, step.status) for step in outcome.steps] == [("0001", StepStatus.PENDING),
                                                                      ("0002", StepStatus.PASSED)]
    assert outcome.pending


def test_an_issue_merged_by_the_queue_is_not_released_again_in_the_same_run(stages, make_runtime, kit):
    runtime = make_runtime()
    add_issue(runtime, "0001", "releasing")

    def merge(runtime: Any) -> list[Any]:
        add_issue(runtime, "0001", "accepting")  # 队列合并后转入验收
        return [kit.stepped("0001", "release.merge", None)]

    stages.queue = merge
    run(runtime, stage="release")
    assert stages.calls == [("queue", None)]


def test_a_given_issue_in_release_is_released_directly(stages, make_runtime):
    runtime = make_runtime()
    add_issue(runtime, "0001", "releasing")
    run(runtime, subject="0001")
    assert stages.calls == [("release", "0001")]  # 不经队列；也不进实施


# 暂停、运行锁、触发条件


def test_a_paused_workspace_starts_no_run_and_a_run_stops_after_the_current_step(stages, make_runtime):
    runtime = make_runtime()
    recovery.pause(runtime.workspace, runtime.clock)
    outcome = run(runtime)
    assert outcome.status is RunStatus.REFUSED and "暂停" in outcome.reason
    assert stages.calls == [] and runs.latest(runtime.conn) is None
    recovery.resume(runtime.workspace)

    def pause_during(runtime: Any, only: Any) -> Any:
        recovery.pause(runtime.workspace, runtime.clock)
        return SimpleNamespace(results=[], new=[], regressed=[], skipped={}, disabled={})

    stages.collect = pause_during
    outcome = run(make_runtime(run="R-20261008T030001Z-run"))
    assert stages.names() == ["collect"]
    assert outcome.halted == "已暂停" and outcome.status is RunStatus.DONE


def test_a_halted_quota_refuses_the_run(stages, make_runtime):
    runtime = make_runtime()
    runtime.agents.quota = SimpleNamespace(halted_until=lambda: NOW + timedelta(hours=2))
    outcome = run(runtime)
    assert outcome.status is RunStatus.REFUSED and "额度" in outcome.reason


def test_a_busy_run_lock_is_skipped_and_recorded_for_scheduled_runs(stages, make_runtime):
    runtime = make_runtime()
    ready(runtime)
    holder = {"pid": 4242, "host": "other-host", "acquiredAt": format_iso(NOW), "heartbeatAt": format_iso(NOW)}
    runtime.workspace.run_lock.parent.mkdir(parents=True, exist_ok=True)
    runtime.workspace.run_lock.write_text(json.dumps(holder))
    outcome = run(runtime, trigger="schedule")
    assert outcome.status is RunStatus.REFUSED and stages.calls == []
    assert runs.get(runtime.conn, runtime.run).status == "skipped"
    manual = make_runtime(run="R-20261008T030001Z-run")
    assert run(manual).status is RunStatus.REFUSED
    assert runs.get(manual.conn, manual.run) is None  # 手动触发：用户当场看到，不另记


def test_scheduled_runs_need_a_ready_project_and_the_window(stages, make_runtime):
    runtime = make_runtime()
    outcome = run(runtime, trigger="schedule")
    assert outcome.status is RunStatus.SKIPPED and "就绪" in outcome.reason
    ready(runtime)
    closed = make_runtime({"schedule": {"window": {"from": "00:00", "to": "07:00", "days": []}}},
                          run="R-20261008T030001Z-run")
    outcome = run(closed, trigger="schedule")
    assert outcome.status is RunStatus.SKIPPED and "时段" in outcome.reason
    assert run(closed, trigger="manual").status is RunStatus.DONE  # 手动触发不看时段
    assert runs.get(runtime.conn, runtime.run) is None


def test_missed_wakes_are_counted_from_the_last_wake(stages, make_runtime):
    runtime = make_runtime()
    ready(runtime)
    state.put(runtime.conn, schedule.WAKE_KEY, format_iso(NOW - timedelta(hours=1)), runtime.clock)
    run(runtime, trigger="schedule")
    assert state.get(runtime.conn, schedule.MISSED_KEY) == 3  # 15m 一次：中间漏了 3 次
    assert state.get(runtime.conn, schedule.WAKE_KEY) == format_iso(NOW)


def test_dry_run_lists_the_plan_and_writes_nothing(stages, make_runtime):
    runtime = make_runtime()
    add_issue(runtime, "0004", "todo")
    add_issue(runtime, "0001", "releasing")
    outcome = run(runtime, dry_run=True)
    assert [(item.stage, item.subject) for item in outcome.planned] == [
        ("collect", "collect.alerts"), ("implement", "0004"), ("release", "0001"), ("retro", None)]
    assert stages.calls == [] and runs.latest(runtime.conn) is None
    assert not runtime.workspace.run_lock.exists()


# 给 status 与 watch 的


def test_in_window_follows_the_hours_and_days(kit):
    settings = window(kit, "00:00", "07:00", ["thu"])
    assert schedule.in_window(settings, datetime(2026, 10, 8, 3, 0, tzinfo=UTC), UTC)  # 星期四
    assert not schedule.in_window(settings, datetime(2026, 10, 8, 7, 0, tzinfo=UTC), UTC)  # 结束时刻不含
    assert not schedule.in_window(settings, datetime(2026, 10, 9, 3, 0, tzinfo=UTC), UTC)  # 星期五


def test_a_window_across_midnight_belongs_to_the_day_it_starts(kit):
    settings = window(kit, "22:00", "06:00", ["fri"])
    assert schedule.in_window(settings, datetime(2026, 10, 9, 23, 0, tzinfo=UTC), UTC)  # 星期五晚上
    assert schedule.in_window(settings, datetime(2026, 10, 10, 5, 0, tzinfo=UTC), UTC)  # 星期六凌晨，算星期五
    assert not schedule.in_window(settings, datetime(2026, 10, 9, 5, 0, tzinfo=UTC), UTC)  # 星期五凌晨，算星期四
    assert not schedule.in_window(settings, datetime(2026, 10, 10, 23, 0, tzinfo=UTC), UTC)


def test_next_run_is_the_next_aligned_wake_inside_the_window(kit):
    settings = window(kit, "00:00", "07:00", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    assert schedule.next_run(settings, datetime(2026, 10, 8, 3, 7, tzinfo=UTC), UTC) == datetime(
        2026, 10, 8, 3, 15, tzinfo=UTC)
    assert schedule.next_run(settings, datetime(2026, 10, 8, 3, 15, tzinfo=UTC), UTC) == datetime(
        2026, 10, 8, 3, 30, tzinfo=UTC)
    assert schedule.next_run(settings, datetime(2026, 10, 8, 6, 50, tzinfo=UTC), UTC) == datetime(
        2026, 10, 9, 0, 0, tzinfo=UTC)
    friday = window(kit, "00:00", "07:00", ["fri"])
    assert schedule.next_run(friday, datetime(2026, 10, 8, 3, 7, tzinfo=UTC), UTC) == datetime(
        2026, 10, 9, 0, 0, tzinfo=UTC)
    assert schedule.next_run(window(kit, "00:00", "07:00", []), datetime(2026, 10, 8, 3, 7, tzinfo=UTC), UTC) is None


def test_the_collect_summary_lists_the_sources_that_are_not_enabled():
    result = SimpleNamespace(results=[SimpleNamespace(source="collect.static")], new=["P-0001"], regressed=[],
                             skipped={"collect.alerts": "未到点"}, disabled={"collect.access_log": "没有日志"})
    assert schedule._collected(result) == ("跑了 1 个来源，新问题 1、回归 0；跳过 1 个；"
                                           "未启用 collect.access_log(没有日志)")


def test_startup_restores_read_only_worktrees_locked_by_dead_processes(stages, make_runtime, tmp_path):
    runtime = make_runtime()
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "a.py").write_text("x", encoding="utf-8")
    marker = runtime.workspace.worktrees_dir / "readonly-assess.json"
    worktrees.lock_readonly(tree, marker, runtime.clock)
    assert not os.access(tree / "src", os.W_OK)
    run(runtime)  # 持有进程仍在：不动
    assert marker.exists()
    later = make_runtime(run="R-20261008T030001Z-run")
    schedule.run(later, trigger="manual", host=HOST, alive=lambda pid: False)
    assert os.access(tree / "src", os.W_OK) and not marker.exists()
