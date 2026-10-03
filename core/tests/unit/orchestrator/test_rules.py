from datetime import timedelta, timezone
from types import SimpleNamespace

from orchestrator_world import FakeModules
from pipeline_world import NOW, make_signal, make_world, save_issue
from triage_world import make_triage_world, store_problem, store_triaged

from tightrein.domain.enums import IssueStatus, RunStage, RunStatus, Stage
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.run import Run
from tightrein.orchestrator import runlog, rules
from tightrein.orchestrator.rules import RunContext, RunRequest
from tightrein.store.repos import runs, schedule_state
from tightrein.pipeline.common.deploys import UNCONFIGURED as DEPLOY_UNCONFIGURED

TICK = {"weekdays": [1, 2, 3, 4, 5], "minutes": [0]}

TTL = timedelta(minutes=120)


def context(world, modules, request=RunRequest()):
    loop = runlog.begin(world.conn, world.clock, world.events, TTL)
    return RunContext(modules, world.conn, world.layout, world.config, world.clock, timezone.utc, loop, request,
                      runlog.Pacer(world.clock, modules.wait_until))


def executed(ctx):
    return [outcome.name for outcome in ctx.outcomes if outcome.executed]


def test_only_steps_with_something_to_do_run(tmp_path):
    world = make_world(tmp_path)
    modules = FakeModules(world)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    assert executed(ctx) == ["deployments", "learn-lessons", "weekly", "retention", "health"]
    assert modules.names() == ["collect.deployments", "learn.lessons", "learn.report", "purge", "learn.health"]
    assert schedule_state.get(world.conn, "weekly").last_status is RunStatus.OK
    modules.calls.clear()
    again = context(world, modules)
    rules.run_steps(again)
    assert "retention" not in executed(again) and "weekly" not in executed(again)


def test_without_deploy_detection_the_step_only_notes_it(tmp_path):
    world = make_world(tmp_path, target=None)
    modules = FakeModules(world, deploy_source=False)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    assert "collect.deployments" not in modules.names() and DEPLOY_UNCONFIGURED in ctx.notes


def test_state_triggers_triage_issue_and_tracking(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001")
    store_triaged(world, "P-0002", make_signal(2), fingerprint="second")
    save_issue(world.conn, "0007", IssueStatus.PENDING_MERGE)
    modules = FakeModules(world)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    assert {"triage", "issue", "release-track"} <= set(executed(ctx))
    assert {"triage.run", "issue.create", "release.track"} <= set(modules.names())


def test_the_github_mirror_step_runs_only_for_the_github_tracker(tmp_path):
    world = make_world(tmp_path, issues={"tracker": "github"})
    skipped = SimpleNamespace(skipped="owner/name 是公开仓库")
    modules = FakeModules(world, issue_mirror=skipped)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    assert "issue-mirror" in executed(ctx) and "issue.mirror" in modules.names()
    assert {"source": "issue-mirror", "reason": "owner/name 是公开仓库", "log": None} in ctx.anomalies


def test_a_failing_step_is_recorded_and_later_steps_still_run(tmp_path):
    world = make_world(tmp_path)
    modules = FakeModules(world, learn_lessons=RuntimeError("执行器不可用"))
    ctx = context(world, modules)
    rules.run_steps(ctx)
    failed = [outcome for outcome in ctx.outcomes if outcome.status is RunStatus.FAILED]
    assert [outcome.name for outcome in failed] == ["learn-lessons"]
    assert ctx.anomalies[0]["source"] == "learn-lessons" and "执行器不可用" in ctx.anomalies[0]["reason"]
    assert "health" in executed(ctx)


def test_triage_that_cannot_start_marks_the_step_failed(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001")
    blocked = SimpleNamespace(run=None, items=[], skipped=[], message="锁定", summary="", blocked="只读 worktree 被锁定")
    modules = FakeModules(world, triage_run=blocked)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    failed = [outcome.name for outcome in ctx.outcomes if outcome.status is RunStatus.FAILED]
    assert failed == ["triage"] and "分诊没有开始：只读 worktree 被锁定" in ctx.anomalies[0]["reason"]


def test_child_runs_are_adopted_and_failures_mark_the_step(tmp_path):
    world = make_world(tmp_path)

    def lessons():
        runs.save(world.conn, Run("R-20261005-030001-learn", RunStage.LEARN, NOW, RunStatus.FAILED))

    modules = FakeModules(world, learn_lessons=lessons)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    outcome = next(item for item in ctx.outcomes if item.name == "learn-lessons")
    assert (outcome.run_ids, outcome.status) == (["R-20261005-030001-learn"], RunStatus.FAILED)
    assert runs.get(world.conn, "R-20261005-030001-learn").parent_run_id == ctx.loop.id


def test_scheduled_tasks_run_their_command_and_aggregate_new_collect_runs(tmp_path):
    task = {"name": "archive", "days": "daily", "at": ["02:00"], "command": "collect --probe incidental"}
    world = make_world(tmp_path, schedule={"tick": TICK, "tasks": [task]})

    def command(argv):
        runs.save(world.conn, Run("R-20261005-030000-collect-incidental", RunStage.COLLECT, NOW, RunStatus.OK,
                                  probe=ProbeKind.INCIDENTAL))
        return 0

    modules = FakeModules(world, run_command=command)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    assert modules.commands == [["collect", "--probe", "incidental"]]
    assert modules.names().count("aggregate.run") == 1
    request = next(args for key, args in modules.calls if key == "aggregate.run")[0]
    assert request.select == ("run:R-20261005-030000-collect-incidental",)
    state = schedule_state.get(world.conn, "archive")
    assert (state.last_status, state.last_run_id) == (RunStatus.OK, ctx.loop.id)


def test_a_chain_runs_only_its_modules(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001")
    modules = FakeModules(world)
    ctx = context(world, modules, RunRequest(chain=(Stage.TRIAGE, Stage.ISSUE), subject="P-0001"))
    rules.run_steps(ctx)
    assert executed(ctx) == ["triage", "issue", "health"]
    triage_request = next(args for key, args in modules.calls if key == "triage.run")[0]
    assert triage_request.select == ("P-0001",)


def test_same_stage_calls_move_to_the_next_second(tmp_path):
    world = make_world(tmp_path)
    modules = FakeModules(world)
    ctx = context(world, modules)
    ctx.call("learn", lambda: None)
    ctx.call("learn", lambda: None)
    assert [moment.second for moment in modules.waits] == [1]


def collect_requests(modules):
    return [args[0] for name, args in modules.calls if name == "collect.run"]


def test_sources_list_disabled_methods_and_run_none_of_them(tmp_path):
    world = make_world(tmp_path)
    modules = FakeModules(world)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    assert "sources" not in executed(ctx) and collect_requests(modules) == []
    assert ("未启用的采集方法：platform-errors(未配置)；access-log(未配置)；alerts(未配置)；project-probe(未配置)"
            in ctx.notes)


def test_platform_sources_run_when_their_interval_has_passed(tmp_path):
    world = make_world(tmp_path, sources={"alerts": {"every": "1h"}})
    disabled = {"access-log": "未配置", "project-probe": "未配置"}
    runs.save(world.conn, Run("R-20261005-024500-collect-alerts", RunStage.COLLECT, NOW - timedelta(minutes=15),
                              RunStatus.OK, probe=ProbeKind.ALERTS))
    runs.save(world.conn, Run("R-20261005-010000-collect-platform-errors", RunStage.COLLECT,
                              NOW - timedelta(hours=2), RunStatus.OK, probe=ProbeKind.PLATFORM_ERRORS))
    modules = FakeModules(world, disabled=disabled)
    ctx = context(world, modules)
    rules.run_steps(ctx)
    assert "sources" in executed(ctx)
    assert [request.probe for request in collect_requests(modules)] == [ProbeKind.PLATFORM_ERRORS]
    assert "未启用的采集方法：access-log(未配置)；project-probe(未配置)" in ctx.notes
    world.clock.advance(timedelta(hours=1))
    modules.calls.clear()
    rules.run_steps(context(world, modules))
    assert [request.probe for request in collect_requests(modules)] == [ProbeKind.PLATFORM_ERRORS, ProbeKind.ALERTS]


def test_failed_runs_do_not_count_and_new_sources_run_at_once(tmp_path):
    world = make_world(tmp_path)
    runs.save(world.conn, Run("R-20261005-025000-collect-alerts", RunStage.COLLECT, NOW - timedelta(minutes=10),
                              RunStatus.FAILED, probe=ProbeKind.ALERTS))
    modules = FakeModules(world, disabled={"platform-errors": "未配置", "access-log": "未配置",
                                           "project-probe": "未配置"})
    rules.run_steps(context(world, modules))
    assert [request.probe for request in collect_requests(modules)] == [ProbeKind.ALERTS]


def test_project_probes_run_when_any_registered_probe_is_due(tmp_path):
    from tightrein.store.repos import probe_states
    from tightrein.store.repos.probe_states import ProbeState

    probes = [{"name": "daily-import", "command": ["{python}", "probes/daily_import.py"], "every": "1d"}]
    world = make_world(tmp_path, sources={"project-probes": probes})
    modules = FakeModules(world, disabled={"platform-errors": "未配置", "access-log": "未配置", "alerts": "未配置"})
    probe_states.save(world.conn, ProbeState("daily-import", NOW - timedelta(hours=2)))
    rules.run_steps(context(world, modules))
    assert collect_requests(modules) == []
    probe_states.save(world.conn, ProbeState("daily-import", NOW - timedelta(days=1)))
    rules.run_steps(context(world, modules))
    assert [request.probe for request in collect_requests(modules)] == [ProbeKind.PROJECT_PROBE]


def test_event_runs_also_collect_due_sources(tmp_path):
    world = make_world(tmp_path)
    modules = FakeModules(world, disabled={"access-log": "未配置", "project-probe": "未配置", "platform-errors": "未配置"})
    ctx = context(world, modules, RunRequest(scheduled=True, events=True))
    rules.run_steps(ctx)
    assert "sources" in executed(ctx)
    assert [request.probe for request in collect_requests(modules)] == [ProbeKind.ALERTS]
