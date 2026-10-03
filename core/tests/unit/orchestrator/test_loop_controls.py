"""暂停、预算、事件运行、修复的顺序与并发、熔断(redesign/09-loop.md 第 3、5、6 节)。"""

from dataclasses import replace
from datetime import timedelta, timezone

from orchestrator_world import FakeModules
from pipeline_world import NOW, make_world, save_issue

import pytest

from tightrein.domain.enums import IssuePhase, IssueStatus, RunStage, RunStatus, Severity, Stage, Treatment
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.run import Run
from tightrein.orchestrator import breaker, pause, rules, runlog
from tightrein.orchestrator.rules import RunContext, RunRequest
from tightrein.orchestrator.service import Orchestrator
from tightrein.store.repos import breaker_counts, budget_usage, issues, runs

TTL = timedelta(minutes=120)


def context(world, modules, request=RunRequest(), **values):
    loop = runlog.begin(world.conn, world.clock, world.events, TTL)
    return RunContext(modules, world.conn, world.layout, world.config, world.clock, timezone.utc, loop, request,
                      runlog.Pacer(world.clock, modules.wait_until), **values)


def executed(ctx):
    return [outcome.name for outcome in ctx.outcomes if outcome.executed]


def orchestrator(world, modules, flag=None):
    return Orchestrator(modules, layout=world.layout, conn=world.conn, config=world.config, clock=world.clock,
                        events=world.events, notifier=None, zone=timezone.utc, alive=lambda pid: False,
                        host="this-host", pause_flag=flag)


def test_a_paused_workspace_starts_no_run_and_a_run_stops_after_the_current_step(tmp_path):
    world = make_world(tmp_path)
    flag = tmp_path / "state" / "paused"
    pause.pause_global(flag, world.clock)
    with pytest.raises(pause.Paused):
        orchestrator(world, FakeModules(world), flag).run(RunRequest())
    assert runs.find(world.conn, stage=RunStage.LOOP) == []
    assert pause.resume_global(flag) and not flag.exists()

    def pause_now():
        pause.pause_workspace(world.conn, world.clock)

    modules = FakeModules(world, collect_deployments=pause_now)
    ctx = context(world, modules, paused=lambda: pause.reason(world.conn, flag))
    rules.run_steps(ctx)
    assert executed(ctx) == ["deployments"] and ctx.halted == {"pause": pause.WORKSPACE}
    assert pause.resume_workspace(world.conn)


def test_the_budget_stops_steps_that_call_models(tmp_path):
    world = make_world(tmp_path, budget={"perDayUsd": 1.0})
    budget_usage.add(world.conn, Stage.TRIAGE, NOW.date(), 1.5, 10, 10, False, world.clock)
    ctx = context(world, FakeModules(world), budget_baseline=0.0)
    rules.run_steps(ctx)
    assert "learn-lessons" not in executed(ctx) and "deployments" in executed(ctx) and "health" in executed(ctx)
    assert ctx.halted["budget"].startswith("每天的费用已达上限：已用 1.50 美元")


def test_the_weekly_budget_can_be_a_share_of_the_subscription(tmp_path):
    from tightrein.runner.limits import BudgetLimits, GlobalBudget

    world = make_world(tmp_path, budget={"perWeekPercent": 50, "subscriptionWeekUsd": 4, "perRunUsd": 3})
    limits = BudgetLimits.from_config(world.config)
    assert (limits.per_week, limits.per_run) == (2.0, 3.0)
    budget_usage.add(world.conn, Stage.FIX, NOW.date() - timedelta(days=NOW.weekday()), 2.0, 1, 1, False,
                     world.clock)
    total = GlobalBudget(world.conn, world.clock, limits)
    assert total.exceeded().startswith("每周") and total.exceeded(run_baseline=2.0).startswith("每周")
    budget_usage.add(world.conn, Stage.FIX, NOW.date(), 3.0, 1, 1, False, world.clock)
    assert total.exceeded(run_baseline=2.0).startswith("本次运行")


def test_events_runs_only_check_events(tmp_path):
    world = make_world(tmp_path)
    runs.save(world.conn, Run("R-20261005-010000-collect-static", RunStage.COLLECT, NOW, RunStatus.OK,
                              probe=ProbeKind.STATIC, target_commit="a" * 40))
    modules = FakeModules(world, main_head="b" * 40)
    ctx = context(world, modules, RunRequest(scheduled=True, events=True))
    rules.run_steps(ctx)
    assert executed(ctx) == ["deployments", "commit-patrol"]
    [request] = [args[0] for name, args in modules.calls if name == "collect.run"]
    assert (request.probe, request.level.value, request.commit) == (ProbeKind.STATIC, "incremental", "b" * 40)


def test_tick_runs_fully_at_the_fixed_time_and_checks_events_otherwise(tmp_path):
    world = make_world(tmp_path, schedule={"tick": {"weekdays": [1, 2, 3, 4, 5], "minutes": [0]},
                                           "runAt": ["09:00"]})
    world.clock.advance(timedelta(hours=9, minutes=5) - timedelta(hours=NOW.hour, minutes=NOW.minute))
    modules = FakeModules(world)
    first = orchestrator(world, modules).tick()
    assert first.kind == "full" and "learn.lessons" in modules.names()
    modules.calls.clear()
    world.clock.advance(timedelta(minutes=15))
    second = orchestrator(world, modules).tick()
    assert second.kind == "events" and "learn.lessons" not in modules.names()
    pause.pause_workspace(world.conn, world.clock)
    assert orchestrator(world, modules).tick().kind == "paused"


def test_new_fixes_wait_for_a_free_slot_and_urgent_ones_go_first(tmp_path):
    world = make_world(tmp_path, gates={"fix-session": "auto"})
    for issue_id, treatment, severity in (("0001", Treatment.SCHEDULED, Severity.P2),
                                          ("0002", Treatment.IMMEDIATE, Severity.P1),
                                          ("0003", Treatment.SCHEDULED, Severity.P1)):
        record = issues.get(world.conn, save_issue(world.conn, issue_id, IssueStatus.TODO).id)
        issues.save(world.conn, replace(record, issue=replace(record.issue, treatment=treatment, severity=severity)))
    ctx = context(world, FakeModules(world))
    assert [issue.id for issue in rules.unattended_issues(ctx)] == ["0002"]
    save_issue(world.conn, "0004", IssueStatus.IN_PROGRESS, phase=IssuePhase.FIX)
    assert [issue.id for issue in rules.unattended_issues(ctx)] == ["0004"]
    save_issue(world.conn, "0004", IssueStatus.IN_PROGRESS, phase=IssuePhase.VERIFY)
    assert [issue.id for issue in rules.unattended_issues(context(world, FakeModules(world)))] == ["0002", "0004"]


def test_the_breaker_trips_after_repeated_failures_and_resets_on_progress(tmp_path):
    world = make_world(tmp_path)
    tripped = []
    fuse = breaker.Breaker(world.conn, world.clock, world.config, lambda subject, reason: tripped.append(subject))
    assert fuse.observe("0007", "release", breaker.FAILED, "推送被拒") is None
    assert fuse.observe("0007", "release", breaker.PROGRESS, "") is None
    assert breaker_counts.get(world.conn, "0007") is None
    for _ in range(2):
        assert fuse.observe("0007", "release", breaker.FAILED, "推送被拒") is None
    reason = fuse.observe("0007", "release", breaker.FAILED, "推送被拒")
    assert reason.startswith("熔断：release 连续失败 3 次") and tripped == ["0007"]
    assert breaker_counts.get(world.conn, "0007").failures == 0
    for _ in range(2):
        fuse.observe("0008", "verify local", breaker.UNCHANGED, "等待")
    assert fuse.observe("0008", "verify local", breaker.UNCHANGED, "等待").startswith("熔断：verify local 连续 3 次")


def test_an_onboarding_workspace_only_checks_its_checklist(tmp_path):
    world = make_world(tmp_path)

    class Flow:
        checked = 0

        def active(self):
            return True

        def check(self):
            Flow.checked += 1
            return type("Report", (), {"summary": "接入中：还差 2 项需要回答"})()

        def digest_lines(self):
            return ["demo：接入中，还差 2 项需要回答"]

        def status_line(self):
            return "demo：接入中，还差 2 项需要回答"

    modules = FakeModules(world)
    service = Orchestrator(modules, layout=world.layout, conn=world.conn, config=world.config, clock=world.clock,
                           events=world.events, notifier=None, zone=timezone.utc, alive=lambda pid: False,
                           host="this-host", onboarding=Flow())
    report = service.run(RunRequest())
    assert [step["name"] for step in report.outputs["steps"]] == ["recovery", "onboarding"] and modules.calls == []
    assert "demo：接入中，还差 2 项需要回答" in report.report.read_text(encoding="utf-8")
    assert service.status()["onboarding"] == "demo：接入中，还差 2 项需要回答" and Flow.checked == 1
