from dataclasses import replace
from datetime import date, timedelta

import pytest
from fix_world import make_fix_world
from pipeline_world import NOW, make_signal
from triage_world import make_triage_world, store_problem, store_triaged

from tightrein.domain.enums import IssuePhase, HandoffStatus, IssueEvent, IssueStatus, RunStage, Stage
from tightrein.orchestrator import resume
from tightrein.orchestrator.resume import ISSUE, PROBLEM, Resumer, StepResult, SubjectRef
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.store import locks
from tightrein.store.locks import Holder
from tightrein.store.repos import handoffs, problems

TTL = timedelta(minutes=120)


class Modules:
    """按命令应答的模块替身：triage 把问题写成已分诊(提 Issue)，issue create 真正创建 Issue。"""

    def __init__(self, world, answers=None):
        self.world = world
        self.answers = dict(answers or {})
        self.calls = []

    def __call__(self, target, step):
        self.calls.append((target.id, step.command))
        if step.command in self.answers:
            return self.answers[step.command]
        if step.command == "triage":
            problem = problems.get(self.world.conn, target.id)
            store_triaged(self.world, target.id, make_signal(2), fingerprint=problem.fingerprint)
            return StepResult(HandoffStatus.OK, "已分诊：提 Issue")
        if step.command == "issue create":
            world = self.world
            run = IssueService(IssueDeps(world.layout, world.config, world.conn, world.clock, world.events,
                                         snapshot=world.worktree)).create((target.id,))
            return StepResult(HandoffStatus.OK, f"已创建 Issue {run.items[0].issue_id}")
        raise AssertionError(f"没有准备 {step.command}")


def resumer(world, modules, **options):
    return Resumer(world.conn, world.clock, modules, lock_ttl=TTL, **options)


def test_identify_and_select(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001")
    assert resume.identify(world.conn, "P-0001") == SubjectRef(PROBLEM, "P-0001")
    with pytest.raises(LookupError, match="0007"):
        resume.identify(world.conn, "7")
    with pytest.raises(ValueError):
        resume.identify(world.conn, "订单")
    assert resume.select(world.conn, ["status:新发现"]) == [SubjectRef(PROBLEM, "P-0001")]
    assert resume.select(world.conn, ["probe:api-fuzz", "P-0001"]) == [SubjectRef(PROBLEM, "P-0001")]
    assert resume.select(world.conn, [f"run:{make_signal().run_id}"]) == [SubjectRef(PROBLEM, "P-0001")]
    with pytest.raises(ValueError):
        resume.select(world.conn, ["status:不存在"])


def test_next_views_follow_the_state_table(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001")
    view = resume.next_view(world.conn, SubjectRef(PROBLEM, "P-0001"))
    assert (view.status, view.command, view.step.can_continue) == ("new", "tightrein triage --select P-0001", True)
    store_triaged(world, "P-0002", make_signal(2), fingerprint="second")
    view = resume.next_view(world.conn, SubjectRef(PROBLEM, "P-0002"))
    assert view.command == "tightrein issue create --select P-0002"
    issue_id = IssueService(IssueDeps(world.layout, world.config, world.conn, world.clock, world.events,
                                      snapshot=world.worktree)).create(("P-0002",)).items[0].issue_id
    view = resume.next_view(world.conn, SubjectRef(PROBLEM, "P-0002"))
    assert view.target == SubjectRef(ISSUE, issue_id) and view.status == "needs-decision"
    assert (view.command, view.to_dict()["gate"], view.step.can_continue) == (
        f"tightrein approve {issue_id.lstrip('0')}", "issue-approval", False)


def test_continue_runs_until_the_next_gate(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001")
    modules = Modules(world)
    report = resumer(world, modules).continue_([SubjectRef(PROBLEM, "P-0001")])
    assert modules.calls == [("P-0001", "triage"), ("P-0001", "issue create")]
    assert [item.message for item in report.progress] == ["已分诊：提 Issue", "已创建 Issue 0001"]
    stop = report.stops[0]
    assert (stop.gate, stop.status, stop.command, stop.failed) == ("issue-approval", "needs-decision",
                                                                   "tightrein approve 1", False)


def test_until_stops_before_later_modules(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001")
    report = resumer(world, Modules(world), until=Stage.TRIAGE).continue_([SubjectRef(PROBLEM, "P-0001")])
    assert report.stops[0].reason == resume.REACHED
    assert report.stops[0].command == "tightrein issue create --select P-0001"


def test_blocked_and_failed_results_stop(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001")
    failed = Modules(world, {"triage": StepResult(HandoffStatus.FAILED, "取证失败")})
    stop = resumer(world, failed).continue_([SubjectRef(PROBLEM, "P-0001")]).stops[0]
    assert (stop.failed, stop.reason) == (True, "取证失败")
    blocked = Modules(world, {"triage": StepResult(HandoffStatus.BLOCKED, "先执行 worktree sync")})
    stop = resumer(world, blocked).continue_([SubjectRef(PROBLEM, "P-0001")]).stops[0]
    assert (stop.gate, stop.reason, stop.failed) == (None, "先执行 worktree sync", False)
    unchanged = Modules(world, {"triage": StepResult(HandoffStatus.OK, "预算用尽，留到下一次")})
    stop = resumer(world, unchanged).continue_([SubjectRef(PROBLEM, "P-0001")]).stops[0]
    assert stop.reason.startswith(resume.UNCHANGED)


def test_pending_operations_stop_or_are_confirmed_in_a_terminal(tmp_path):
    world = make_fix_world(tmp_path)
    ref = SubjectRef(ISSUE, world.issue_id)
    world.event(IssueEvent.FIX_STARTED, actor="fix")
    world.event(IssueEvent.FIX_DONE, actor="fix")
    world.event(IssueEvent.VERIFY_PASSED, actor="verify")
    assert world.issue().phase is IssuePhase.SUBMIT
    pending = StepResult(HandoffStatus.BLOCKED, "提交待确认", operation="OP-0001")
    stop = resumer(world, Modules(world, {"release": pending})).continue_([ref]).stops[0]
    assert (stop.gate, stop.operation) == ("pending-operation", "OP-0001")
    confirmed = []

    def confirm(operation_id):
        confirmed.append(operation_id)
        world.event(IssueEvent.PR_CREATED, actor="release")
        return True

    report = resumer(world, Modules(world, {"release": pending}), confirm=confirm).continue_([ref])
    assert confirmed == ["OP-0001"] and report.stops[0].gate == "pr-review"


def test_interactive_fix_and_locks(tmp_path):
    world = make_fix_world(tmp_path)
    ref = SubjectRef(ISSUE, world.issue_id)
    stop = resumer(world, Modules(world)).continue_([ref]).stops[0]
    assert (stop.gate, stop.command) == ("interactive-fix", f"tightrein fix start {world.issue_id.lstrip('0')}")
    locks.acquire(world.conn, world.issue_id, world.clock, TTL, holder=Holder(1, "other-host"))
    report = resumer(world, Modules(world)).continue_([ref])
    assert report.stops == [] and report.skipped[0][0] == world.issue_id


def test_restart_from_fix_verify_and_triage(tmp_path):
    world = make_fix_world(tmp_path)
    ref = SubjectRef(ISSUE, world.issue_id)
    world.event(IssueEvent.FIX_STARTED, actor="fix")
    world.event(IssueEvent.FIX_DONE, actor="fix")
    run = stage_runs.begin(RunStage.FIX, world.layout, world.conn, world.clock, world.events)
    outputs = {"issueId": world.issue_id, "branch": "cty/fix-order-500", "worktree": str(world.worktree),
               "baseCommit": "1" * 40}
    run.handoff(RunStage.FIX, world.issue_id, HandoffStatus.OK, outputs, "fix done")
    resume.restart(world.conn, world.layout, world.clock, world.config, ref, Stage.FIX, lambda problem: None)
    assert world.issue().status is IssueStatus.TODO
    assert handoffs.get(world.conn, RunStage.FIX, world.issue_id).stale_at == NOW
    world.event(IssueEvent.FIX_STARTED, actor="fix")
    world.event(IssueEvent.FIX_DONE, actor="fix")
    world.event(IssueEvent.VERIFY_PASSED, actor="verify")
    resume.restart(world.conn, world.layout, world.clock, world.config, ref, Stage.VERIFY, lambda problem: None)
    assert world.issue().phase is IssuePhase.VERIFY
    retriaged = []
    refs = resume.restart(world.conn, world.layout, world.clock, world.config, ref, Stage.TRIAGE, retriaged.append)
    assert retriaged == ["P-0001"] and refs == [SubjectRef(PROBLEM, "P-0001")]
    with pytest.raises(ValueError):
        resume.restart(world.conn, world.layout, world.clock, world.config, ref, Stage.RELEASE, retriaged.append)


def test_a_manual_issue_cannot_restart_from_triage(tmp_path):
    world = make_fix_world(tmp_path, manual=("按日期筛选订单", "订单列表按日期筛选。\n"))
    ref = SubjectRef(ISSUE, world.issue_id)
    with pytest.raises(ValueError, match="没有关联问题"):
        resume.restart(world.conn, world.layout, world.clock, world.config, ref, Stage.TRIAGE, lambda problem: None)


def test_find_matches_titles_and_locations_within_dates(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001", title="订单查询返回 500")
    store_problem(world, "P-0002", make_signal(2, location="GET /api/Material/7"), fingerprint="x",
                  title="物料接口报错")
    problems.save(world.conn, replace(problems.get(world.conn, "P-0002"), last_seen_at=NOW - timedelta(days=3)))
    found = resume.find(world.conn, "material", limit=20)
    assert [(item.ref.id, item.matched) for item in found] == [("P-0002", ("location",))]
    assert resume.find(world.conn, "物料", since=date(2026, 10, 4), limit=20) == []
    assert [item.ref.id for item in resume.find(world.conn, "订单", limit=20)] == ["P-0001"]
    assert resume.find(world.conn, "订单", kind=ISSUE, limit=20) == []
