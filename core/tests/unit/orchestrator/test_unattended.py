"""gates.fix-session 为 auto 时 continue 与 run 以无人值守方式修复：不启动交互会话，按续接点执行非交互任务；失败时转人工。"""

from datetime import timezone
from types import SimpleNamespace

from orchestrator_world import FakeModules
from test_fix_service import (
    EXECUTED,
    FIXED,
    PASS,
    SERVICE_PATH,
    TEST_CODE,
    TEST_FILE,
    WRITTEN,
    fixing,
    plan_output,
    service,
)

from tightrein.domain.enums import IssuePhase, HandoffStatus, Stage
from tightrein.orchestrator.rules import RunRequest
from tightrein.orchestrator.service import Orchestrator
from tightrein.store.repos import github_mirror

AUTONOMY = {"issue-approve": "auto", "plan-confirm": "auto", "fix-session": "auto"}
MEDIUM = {"sizeTier": "medium"}


class Modules(FakeModules):
    def __init__(self, world, fix, **actions):
        super().__init__(world, **actions)
        self.real_fix = fix

    def fix(self):
        return self.real_fix


def orchestrator(world, modules):
    return Orchestrator(modules, layout=world.layout, conn=world.conn, config=world.config, clock=world.clock,
                        events=world.events, notifier=None, zone=timezone.utc, alive=lambda pid: False,
                        host="this-host")


def test_continue_fixes_without_an_interactive_session(tmp_path):
    world = fixing(tmp_path, gates=AUTONOMY)
    fix = service(world)
    world.runner.edits += [{TEST_FILE: TEST_CODE}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", WRITTEN, EXECUTED)
    modules = Modules(world, fix)
    result = orchestrator(world, modules).continue_([world.issue_id.lstrip("0")], until=Stage.FIX)
    assert world.issue().phase is IssuePhase.VERIFY, result.report.stops
    assert world.runner.sessions == [] and "verify.reproduce" not in modules.names()
    assert world.runner.roles() == ["fix-executor", "fix-executor"]
    assert result.report.stops[0].reason == "已到终点"


def test_failures_hold_the_issue_and_ask_the_user_on_github(tmp_path):
    world = fixing(tmp_path, outputs=MEDIUM, gates=AUTONOMY, issues={"tracker": "github"})
    fix = service(world)
    world.runner.add("fix-planner", plan_output(world))
    world.runner.edits += [{TEST_FILE: TEST_CODE}] + [{SERVICE_PATH: FIXED + "".join(
        f"extra {n}\n" for n in range(510))}] * 2
    world.runner.add("fix-executor", WRITTEN, EXECUTED)
    modules = Modules(world, fix)
    stop = orchestrator(world, modules).continue_([world.issue_id]).report.stops[0]
    assert stop.failed and stop.reason.startswith("无人值守修复停下：改动量仍超出上限")
    assert world.issue().hold.reason == "改动量仍超出上限"
    comments = [item.body for item in github_mirror.unposted(world.conn, world.issue_id)]
    assert comments == []


def test_plans_needing_a_decision_stop_at_the_confirmation_without_a_hold(tmp_path):
    world = fixing(tmp_path, outputs=MEDIUM, gates=AUTONOMY)
    fix = service(world)
    decision = [{"question": "是否保留旧接口", "recommendation": "保留", "reason": "有外部调用"}]
    world.runner.add("fix-planner", plan_output(world, userDecisions=decision))
    stop = orchestrator(world, Modules(world, fix)).continue_([world.issue_id]).report.stops[0]
    assert stop.gate == "fix-plan" and not stop.failed and world.issue().hold is None


def test_without_autonomy_the_interactive_gate_is_unchanged(tmp_path):
    world = fixing(tmp_path)
    stop = orchestrator(world, Modules(world, service(world))).continue_([world.issue_id]).report.stops[0]
    assert stop.gate == "interactive-fix" and world.runner.tasks == []


def test_run_advances_approved_issues(tmp_path):
    world = fixing(tmp_path, outputs=MEDIUM, gates=AUTONOMY)
    fix = service(world)
    world.runner.add("fix-planner", plan_output(world))
    world.runner.edits += [{TEST_FILE: TEST_CODE}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", WRITTEN, EXECUTED).add("fix-reviewer", PASS)
    verified = SimpleNamespace(status=HandoffStatus.BLOCKED, message="本机服务未就绪")
    modules = Modules(world, fix, verify_local=verified)
    report = orchestrator(world, modules).run(RunRequest())
    step = next(item for item in report.outputs["steps"] if item["name"] == "unattended")
    assert step["executed"] and world.issue().phase is IssuePhase.VERIFY
    assert "verify.local" in modules.names()
