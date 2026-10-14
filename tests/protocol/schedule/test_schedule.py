import importlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.assess.issue import transitions
from tightrein.onboard.check import READY_KEY
from tightrein.protocol import recovery, schedule
from tightrein.protocol.git import worktrees
from tightrein.protocol.handoff import Handoff, write
from tightrein.protocol.handoff import Status as HandoffStatus
from tightrein.protocol.naming import FileName, format_iso
from tightrein.protocol.schedule import RunStatus, StepStatus
from tightrein.settings.load import Settings
from tightrein.store.db import connect
from tightrein.store.tables import counters, issues, problems, runs, state

for package in ("tightrein.collect", "tightrein.implement", "tightrein.release", "tightrein.retro"):
    importlib.import_module(package)  # conftest 替换各阶段入口时要在包上设属性：包先导入

HOST = "test-host"
RUN_ID = "R-20261008T030000Z-run"
NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)  # 与 conftest 的时钟相同


def run(runtime: Any, **options: Any) -> schedule.Outcome:
    options.setdefault("trigger", "manual")
    return schedule.run(runtime, host=HOST, alive=lambda pid: True, **options)


def add_issue(runtime: Any, issue_id: str, status: str, severity: str = "P2", held_by: str | None = None,
              **extra: Any) -> None:
    issues.save(runtime.conn, issues.Issue(id=issue_id, status=status, title=f"Issue {issue_id}", kind="bug",
                                           origin="problem", severity=severity, held_by=held_by, extra=extra),
                runtime.clock)


def add_problem(runtime: Any, problem_id: str, status: str = "new") -> None:
    problems.save(runtime.conn, problems.Problem(id=problem_id, fingerprint=problem_id, source="collect.alerts",
                                                 check_type="alert", status=status, title="出错",
                                                 first_seen=NOW, last_seen=NOW), runtime.clock)


def ready(runtime: Any) -> None:
    state.put(runtime.conn, READY_KEY, format_iso(NOW), runtime.clock)


@contextmanager
def separate(runtime: Any) -> Any:
    """与真实的 isolated 一样，在线程里另开数据库连接(并行实施的测试替换 schedule.isolated)。"""
    from tightrein.protocol.limits import Breaker
    from tightrein.protocol.resources import IssueBudget, Quota

    conn = connect(runtime.workspace.database)
    clock, settings = runtime.clock, runtime.settings
    agents = SimpleNamespace(**{**vars(runtime.agents), "conn": conn, "breaker": Breaker(conn, clock, settings),
                                "quota": Quota(conn, clock, settings), "budget": IssueBudget(conn, clock, settings)})
    try:
        yield SimpleNamespace(**{**vars(runtime), "conn": conn, "agents": agents})
    finally:
        conn.close()


def window(kit: Any, start: str, end: str, days: list[str]) -> Settings:
    return Settings.from_data(kit.DEFAULTS, {"schedule": {"window": {"from": start, "to": end, "days": days}}})


# 按状态推进


def test_a_manual_run_goes_through_every_stage_in_order_and_ends_with_retro(stages, make_runtime):
    runtime = make_runtime()
    outcome = run(runtime)
    assert outcome.status is RunStatus.DONE
    assert stages.names() == ["queue", "collect", "assess_pending", "retro"]  # 先实施、发布已放行的，再采集、评估
    assert stages.calls[1] == ("collect", ["collect.alerts"])  # 手动触发：启用的来源全部交给采集，不看是否到点
    recorded = runs.get(runtime.conn, runtime.run)
    assert (recorded.status, recorded.trigger, recorded.stage) == ("done", "manual", "run")
    assert [step["stage"] for step in recorded.summary["steps"]] == ["collect", "retro"]  # 没有要推进的 Issue


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


def test_a_step_returned_by_the_flow_is_not_a_failure(stages, make_runtime, kit):
    """按流程退回(returned：等前置 Issue、拆分、带原因的否决)不算这一步失败，不计进对象熔断。"""
    runtime = make_runtime()
    stages.assess_pending = lambda runtime: [kit.assessed("P-0001", HandoffStatus.RETURNED.value)]
    outcome = run(runtime, stage="assess")
    assert [(step.subject, step.status) for step in outcome.steps] == [("P-0001", StepStatus.PASSED)]
    assert counters.get(runtime.conn, "breaker.object.P-0001") == 0


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
    assert stages.names() == ["queue", "collect"]
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
        ("implement", "0004"), ("release", "0001"), ("collect", "collect.alerts"), ("retro", None)]
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


def add_touching(runtime: Any, *files: tuple[str, str]) -> None:
    """(编号, 根因文件) 的待修 Issue。"""
    for issue_id, file in files:
        issues.save(runtime.conn, issues.Issue(id=issue_id, status="todo", title=f"Issue {issue_id}", kind="bug",
                                               origin="problem", severity="P2",
                                               extra={"rootCauses": [{"file": file, "line": 1}]}), runtime.clock)


def test_several_unrelated_issues_are_implemented_at_once(stages, make_runtime, monkeypatch):
    """resources.concurrency.sessions 个互不相关的 Issue 同时推进(各在自己的线程里)；根因文件重叠的不同时做，同批全做完
    后不等 refillAfter 接着做，不留到下一次运行。"""
    monkeypatch.setattr(schedule, "REFILL_POLL_S", 0.05)
    monkeypatch.setattr(schedule, "isolated", separate)
    runtime = make_runtime({"resources": {"refillWhenFree": 2, "refillAfter": "1h", "concurrency": {"sessions": 2}}})
    add_touching(runtime, ("0001", "a.py"), ("0002", "a.py"), ("0003", "b.py"))
    together = threading.Barrier(2, timeout=2)  # 0001 与 0003 同时在做才过得去
    guard = threading.Lock()
    active: set[str] = set()
    clashes: list[str] = []

    def implement(runtime, issue):
        with guard:
            if issue == "0002" and "0001" in active:
                clashes.append(issue)
            active.add(issue)
        if issue in ("0001", "0003"):
            together.wait()
        with guard:
            active.discard(issue)
        return SimpleNamespace(subject=issue, point="implement.deliver", status="passed", summary="交付",
                               next_point=None)

    stages.implement = implement
    outcome = run(runtime, stage="implement")
    assert sorted(value for name, value in stages.calls if name == "implement") == ["0001", "0002", "0003"]
    assert not outcome.failed and clashes == []  # 0001、0003 一起做；0002 等 0001 做完才开始


def test_an_issue_touching_the_same_file_is_done_after_the_running_one_in_the_same_run(stages, make_runtime,
                                                                                         monkeypatch):
    """第一批只挑得出一个(另一个改同一个文件)时，它做完就接着做另一个：曾经 0004 与 0028 都改 processors/ai_review.py，
    实施只做了 0028 就结束，0004 等到下一次运行。这一轮做过的不再重复推进。"""
    monkeypatch.setattr(schedule, "REFILL_POLL_S", 0.05)
    monkeypatch.setattr(schedule, "isolated", separate)
    runtime = make_runtime({"resources": {"concurrency": {"sessions": 2}}})
    add_issue(runtime, "0001", "todo", rootCauses=[{"file": "a.py", "line": 1}], plannedFiles=["ai_review.py"])
    add_issue(runtime, "0002", "todo", rootCauses=[{"file": "b.py", "line": 1}], plannedFiles=["ai_review.py"])
    assert [item.id for item in schedule._issues_to_implement(runtime, 2)] == ["0001"]
    run(runtime, stage="implement")
    assert [value for name, value in stages.calls if name == "implement"] == ["0001", "0002"]


def test_after_the_round_is_all_done_no_more_than_refill_max_are_started(stages, make_runtime, monkeypatch):
    """同批全做完时接着开也算在 refillMax 里：第一批就到了上限，改同一文件而没挑的留到下一次运行。"""
    monkeypatch.setattr(schedule, "REFILL_POLL_S", 0.05)
    monkeypatch.setattr(schedule, "isolated", separate)
    runtime = make_runtime({"resources": {"refillMax": 2, "concurrency": {"sessions": 2}}})
    add_touching(runtime, ("0001", "a.py"), ("0002", "a.py"), ("0003", "b.py"))
    run(runtime, stage="implement")
    assert sorted(value for name, value in stages.calls if name == "implement") == ["0001", "0003"]


def test_one_issue_at_a_time_by_default(stages, make_runtime):
    runtime = make_runtime()
    add_issue(runtime, "0001", "todo")
    add_issue(runtime, "0002", "todo")
    run(runtime, stage="implement")
    assert [value for name, value in stages.calls if name == "implement"] == ["0001"]


@pytest.mark.parametrize("refill, while_running", [("0s", True), ("1h", False)])
def test_a_free_slot_is_refilled_after_waiting_while_others_still_run(stages, make_runtime, monkeypatch, refill,
                                                                      while_running):
    """空位不到 refillWhenFree 个时的兜底：同批里先做完的空出位置，等过 refillAfter 而同批还有没做完的，就补一个；
    等不到就等同批全做完再开。"""
    monkeypatch.setattr(schedule, "REFILL_POLL_S", 0.05)
    monkeypatch.setattr(schedule, "isolated", separate)
    runtime = make_runtime({"resources": {"refillAfter": refill, "refillWhenFree": 2, "concurrency": {"sessions": 2}}})
    add_touching(runtime, ("0001", "a.py"), ("0002", "b.py"), ("0003", "c.py"))
    third_started = threading.Event()
    waited: list[bool] = []

    def implement(runtime, issue):
        if issue == "0002":  # 慢的那个：等第三个补进来，等不到就 1 秒后自己结束
            waited.append(third_started.wait(timeout=1))
        if issue == "0003":
            third_started.set()
        return SimpleNamespace(subject=issue, point="implement.deliver", status="passed", summary="交付",
                               next_point=None)

    stages.implement = implement
    run(runtime, stage="implement")
    assert sorted(value for name, value in stages.calls if name == "implement") == ["0001", "0002", "0003"]
    assert waited == [while_running]


def test_issues_waiting_for_prerequisites_are_skipped_until_those_land(stages, make_runtime):
    """blockedBy 中还有没合并的(待修、实施中、已交付等合并)就不挑；合并(验收观察中、已完成)或取消后照常挑。"""
    runtime = make_runtime()
    add_issue(runtime, "0001", "todo", severity="P0", blockedBy=["0002", "0003"])
    add_issue(runtime, "0002", "releasing")
    add_issue(runtime, "0003", "cancelled", closeReason="wont_fix")
    add_issue(runtime, "0004", "todo", severity="P3")
    run(runtime, stage="implement")
    assert [value for name, value in stages.calls if name == "implement"] == ["0004"]
    assert [item.subject for item in run(runtime, stage="implement", dry_run=True).planned] == ["0004"]
    found = issues.get(runtime.conn, "0002")
    issues.save(runtime.conn, replace(found, status="accepting"), runtime.clock)
    assert [item.subject for item in run(runtime, stage="implement", dry_run=True).planned] == ["0001"]


def test_issues_waiting_at_a_gate_without_a_new_decision_are_not_chosen(stages, make_runtime):
    """停在关卡等用户的实施中 Issue 不占位置；有了新决定(implement.waiting_for_user 为假)才挑它。"""
    runtime = make_runtime({"resources": {"concurrency": {"sessions": 2}}})
    add_issue(runtime, "0001", "implementing", severity="P0")
    add_issue(runtime, "0002", "todo")
    stages.waiting = {"0001", "0002"}  # 只对实施中的问：待修的还没开始，不会停在关卡
    assert [item.id for item in schedule._issues_to_implement(runtime, 2)] == ["0002"]
    stages.waiting = set()
    assert [item.id for item in schedule._issues_to_implement(runtime, 2)] == ["0001", "0002"]


def test_issues_that_will_touch_the_same_files_are_not_run_at_the_same_time(make_runtime, tmp_path):
    """根因不同、但评估预估的文件、方案的文件或 worktree 中已改动的文件有重叠的，不同时做(只看根因文件时曾同时改了
    同一个文件，发布时要退回实施)。"""
    runtime = make_runtime({"resources": {"concurrency": {"sessions": 3}}})
    add_issue(runtime, "0001", "todo", rootCauses=[{"file": "a.py", "line": 1}], plannedFiles=["shared.py"])
    add_issue(runtime, "0002", "todo", rootCauses=[{"file": "b.py", "line": 1}], plannedFiles=["shared.py"])
    assert [item.id for item in schedule._issues_to_implement(runtime, 3)] == ["0001"]
    # 方案的文件清单与步骤、修复 worktree 中已改动的文件同样算
    write(runtime.workspace.step_file("0002", FileName("implement.design", "handoff", "json")),
          Handoff(point="implement.design", subject="0002", run=RUN_ID, status=HandoffStatus.PASSED, summary="方案",
                  facts={"files": [{"path": "b.py"}], "steps": [{"file": "tests/test_b.py"}]}))
    add_issue(runtime, "0003", "todo", rootCauses=[{"file": "c.py", "line": 1}], plannedFiles=["tests/test_b.py"])
    tree = tmp_path / "worktree-0004"
    tree.mkdir()
    write(runtime.workspace.step_file("0004", FileName("implement.prepare", "handoff", "json")),
          Handoff(point="implement.prepare", subject="0004", run=RUN_ID, status=HandoffStatus.PASSED, summary="准备",
                  facts={"worktree": str(tree)}))
    add_issue(runtime, "0004", "implementing", rootCauses=[{"file": "d.py", "line": 1}])
    runtime.git = SimpleNamespace(at=lambda path: SimpleNamespace(
        status=lambda: SimpleNamespace(changed_paths=("c.py",) if path == tree else ())))
    found = schedule._issues_to_implement(runtime, 3)
    assert [item.id for item in found] == ["0004", "0001"]  # 0002 与 0001 撞 shared.py；0003 与 0004 撞 c.py
    assert [item.id for item in schedule._issues_to_implement(runtime, 3, busy={"a.py", "d.py"})] == ["0002"]


def add_independent(runtime: Any, count: int) -> None:
    """count 个根因文件互不重叠的待修 Issue(0001 起)。"""
    for number in range(1, count + 1):
        issues.save(runtime.conn, issues.Issue(id=f"{number:04d}", status="todo", title=f"Issue {number}", kind="bug",
                                               origin="problem", severity="P2",
                                               extra={"rootCauses": [{"file": f"{number}.py", "line": 1}]}),
                    runtime.clock)


@pytest.mark.parametrize("sessions, resources, when_free, most", [
    (1, {}, 1, 2), (2, {}, 1, 4), (3, {}, 1, 6),  # 缺省：sessions 的一半取整(至少 1)、2 倍
    (3, {"refillWhenFree": 2, "refillMax": 3}, 2, 3),
])
def test_refill_settings_default_to_a_share_of_the_sessions(make_runtime, sessions, resources, when_free, most):
    refill = schedule._Refill.of(make_runtime({"resources": resources}).settings, sessions)
    assert (refill.when_free, refill.most) == (when_free, most)
    assert refill.count([0.0], 1.0) == (1 if when_free == 1 else 0)  # refillAfter 缺省 3m：单个空位还没等够


def test_slots_freed_together_are_refilled_together(stages, make_runtime, monkeypatch):
    """同时空出 refillWhenFree 个位置才一起补满；只空一个、又没等过 refillAfter 时不补。"""
    monkeypatch.setattr(schedule, "REFILL_POLL_S", 0.05)
    monkeypatch.setattr(schedule, "isolated", separate)
    runtime = make_runtime({"resources": {"refillWhenFree": 2, "refillAfter": "1h", "concurrency": {"sessions": 3}}})
    add_independent(runtime, 5)
    started: list[str] = []
    lone: list[str] = []  # 只空着一个位置的那段时间结束时已开始的
    refilled = threading.Event()

    def implement(runtime, issue):
        started.append(issue)
        if issue == "0002":  # 0001 一开始就做完：只空一个位置，多看几次空位也不补
            threading.Event().wait(0.3)
            lone.extend(started)
        if issue == "0003":  # 等补进来的两个都开始了再结束
            refilled.wait(timeout=2)
        if {"0004", "0005"} <= set(started):
            refilled.set()
        return SimpleNamespace(subject=issue, point="implement.deliver", status="passed", summary="交付",
                               next_point=None)

    stages.implement = implement
    run(runtime, stage="implement")
    assert sorted(lone) == ["0001", "0002", "0003"]
    assert sorted(started) == ["0001", "0002", "0003", "0004", "0005"] and refilled.is_set()


def test_refilling_stops_once_a_round_has_started_refill_max_issues(stages, make_runtime, monkeypatch):
    """一轮开始的 Issue 到 resources.refillMax(缺省 sessions 的 2 倍)就不再补：做完的等着进入发布，不一直补到队列空。"""
    monkeypatch.setattr(schedule, "REFILL_POLL_S", 0.05)
    monkeypatch.setattr(schedule, "isolated", separate)
    runtime = make_runtime({"resources": {"refillAfter": "0s", "concurrency": {"sessions": 2}}})
    add_independent(runtime, 6)
    fourth_done = threading.Event()

    def implement(runtime, issue):
        if issue == "0001":  # 一直做到别的都做完：期间空位照常要补
            fourth_done.wait(timeout=2)
            threading.Event().wait(0.3)  # 再留几次看空位的时间：封顶了就不会再补
        if issue == "0004":
            fourth_done.set()
        return SimpleNamespace(subject=issue, point="implement.deliver", status="passed", summary="交付",
                               next_point=None)

    stages.implement = implement
    run(runtime, stage="implement")
    assert sorted(value for name, value in stages.calls if name == "implement") == ["0001", "0002", "0003", "0004"]
