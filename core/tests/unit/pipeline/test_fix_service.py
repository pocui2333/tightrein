import json
import shutil
from dataclasses import replace

from fix_world import BASE, CONTROLLER, CONTROLLER_PATH, SERVICE, SERVICE_PATH, FakeGit, make_fix_world
from pipeline_world import make_signal
from triage_world import triage_outputs

from tightrein.domain.enums import (
    IssuePhase,
    HandoffStatus,
    IssueEvent,
    IssueStatus,
    OperationExecutor,
    OperationKind,
    OperationStatus,
    ProblemEvent,
    RunnerStatus,
    RunStage,
    Stage,
)
from tightrein.guards.report import Violation
from tightrein.pipeline.checks import project_checks
from tightrein.pipeline.checks.regressions import manifest
from tightrein.pipeline.checks.regressions.repo_test_check import RepoTestCheck
from tightrein.pipeline.checks.regressions.runner import RegressionExecutor
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.fix.service import FixDeps, FixService, ResumePoint
from tightrein.pipeline.fix.steps import context, decisions, route
from tightrein.sources.common.procs import ToolRun
from tightrein.runner.result import RunnerResult
from tightrein.store.files import documents
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.repos import issues, pending_operations, problem_events, regressions, scores
from tightrein.vcs.executor import OperationResult, OperationRunner
from tightrein.vcs.operations import PendingOperation

REQUEST = {"method": "GET", "path": "/api/Order/42", "pathTemplate": "/api/Order/{id}", "query": {}}
FLAGS = {"design": {"flagged": False}, "dataStructure": {"flagged": False}, "publicContract": {"flagged": False}}
FIXED = SERVICE.replace("line 12\n", "line 12 filtered by company\n")
PASS = {"analysis": "逐条核对验收标准。", "mode": "light", "blockers": [], "unverified": [],
        "items": [{"itemId": item, "result": "pass", "reason": "已核对"}
                  for item in ("fix.no-special-case", "fix.acceptance", "fix.deep-review")]}
EXECUTED = {"analysis": "按计划加过滤条件。", "status": "completed", "changedFiles": [SERVICE_PATH], "deviations": [],
            "bigIssue": None, "verification": [{"command": "make test", "output": "3 passed"}], "incidental": [],
            "outOfScope": [], "release": {"scope": "订单", "subject": "订单查询按公司过滤", "why": "其他公司的订单编号会被直接查询。",
                        "prTitle": "按公司过滤订单查询", "problem": "其他公司的订单可以被查到。",
                        "approach": "在查询入口按公司过滤，改动最小。", "limitations": "没有处理历史缓存。"}}
TEST_FILE = "tests/test_order_service.py"
TEST_CODE = "def test_owner():\n    assert filtered_by_company(42)\n"
TEST_COMMAND = f"python -m pytest -q {TEST_FILE}::test_owner"
WRITTEN = {"analysis": "用编号 42 的订单复现越权读取。", "status": "written", "file": TEST_FILE, "command": TEST_COMMAND,
           "location": f"{SERVICE_PATH}:12", "covers": ["只返回本公司的订单"], "reason": None}
PROJECT = {"testPaths": ["tests/"],
           "checks": {"commands": [{"name": "pytest", "cwd": ".", "command": "python -m pytest -q"}]}}


def api_signal():
    return make_signal(1, context={"request": REQUEST, "response": {"status": 500}})


class Launcher:
    """项目检查命令一律通过；复现测试在 OrderService 没有按公司过滤时失败(退出码 1)，passes 为真时总是通过。"""

    def __init__(self, worktree=None, passes=False, base_fails=False):
        self.worktree = worktree
        self.passes = passes
        self.base_fails = base_fails
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        if TEST_FILE in " ".join(command.argv):
            fixed = self.passes or "filtered by company" in (self.worktree / SERVICE_PATH).read_text(encoding="utf-8")
            return ToolRun(0 if fixed else 1)
        return ToolRun(1 if self.base_fails else 0)


def scouting(**changes):
    output = {"analysis": "Get 按编号查询。", "existing": [], "reusable": [], "dataStructure": [], "linkage": [],
              "problems": [], "designIssue": None, "affectedEndpoints": ["GET /api/Order/{id}"], "affectedPages": [],
              "incidental": []}
    output.update(changes)
    return output


def plan_output(world, **changes):
    ctx = context.load(world.conn, world.layout, world.issue_id)
    output = {
        "analysis": "根因在 Get 没有按公司过滤。",
        "summary": "在 OrderService.Get 中按公司过滤",
        "steps": [{"file": SERVICE_PATH, "change": "加过滤条件", "verification": "make test"}],
        "files": [{"path": SERVICE_PATH, "isNew": False, "reason": None}], "estimate": {"files": 1, "lines": 2},
        "split": None, "protectedTouches": [], "flags": FLAGS, "migration": None, "newDependencies": [],
        "deletions": [], "acceptanceMapping": [{"criterion": item, "steps": [1]} for item in ctx.acceptance],
        "userVisibleChange": "无", "affectedEndpoints": ["GET /api/Order/{id}"], "affectedPages": [],
        "notDoing": [], "userDecisions": [],
    }
    output.update(changes)
    # 根因假说的修改位置按文件清单给出(每个要修改的已有文件一处)，测试按需整体替换
    output.setdefault("hypothesis", {
        "cause": "其他公司的用户请求订单 → Get 只按编号查询 → 返回了不属于该公司的订单",
        "evidence": [{"location": f"{SERVICE_PATH}:1", "fact": "查询条件只有编号"}],
        "edits": [{"location": f"{item['path']}:1", "change": "加过滤条件"} for item in output["files"]
                  if not item["isNew"]]})
    return output


def service(world, git=None, layout=None, launcher=None, **changes):
    launcher = launcher or Launcher(world.worktree)
    commands = project_checks.commands(world.config)
    resolve = lambda command, file: project_checks.repro_test_cwd(commands, command, file)
    deps = FixDeps(layout or world.layout, ToolLayout(), world.config, world.conn, world.clock, world.events,
                   world.runner, git or world.git(), launcher, branch_prefix="cty",
                   operations=OperationRunner(world.conn, None, None, None, world.layout, "R-20261005-030000-fix"),
                   executor=RegressionExecutor(world.layout, test=RepoTestCheck(launcher, {}, 60, resolve, (1,))))
    for name, value in changes.items():
        setattr(deps, name, value)
    return FixService(deps)


def fixing(tmp_path, signal=None, outputs=None, manual=None, **config):
    """修复中的 Issue；outputs 改写分诊结论中的任务类型、规模档等。"""
    if outputs is not None:
        import fix_world

        original = fix_world.triage_outputs
        fix_world.triage_outputs = lambda problem_id: triage_outputs(problem_id, **outputs)
        try:
            world = make_fix_world(tmp_path, signal=signal or api_signal(), **{**PROJECT, **config})
        finally:
            fix_world.triage_outputs = original
    else:
        world = make_fix_world(tmp_path, signal=signal or api_signal(), manual=manual, **{**PROJECT, **config})
    world.event(IssueEvent.FIX_STARTED, actor="fix")
    directory = world.layout.fixes_dir(world.issue_id)
    directory.mkdir(parents=True)
    (directory / "workspace.json").write_text(json.dumps(
        {"branch": "cty/fix-order-500", "baseCommit": BASE, "worktree": str(world.worktree)}), encoding="utf-8")
    return world


def handoff(world):
    return stage_runs.latest_outputs(world.conn, world.layout, RunStage.FIX, world.issue_id)


def progress(world):
    return documents.read(world.layout.fixes_dir(world.issue_id) / "progress.md")


def test_lane_a_writes_a_failing_test_then_the_code_in_the_same_session(tmp_path):
    world = fixing(tmp_path)
    fix = service(world)
    planned = fix.plan(world.issue_id)
    assert planned.status is HandoffStatus.OK and world.runner.tasks == []
    assert route.load(world.layout.fixes_dir(world.issue_id)).lane.value == "fast"
    assert fix.resume_point(world.issue_id) is ResumePoint.APPLY
    assert documents.read(world.layout.fixes_dir(world.issue_id) / "plan.md").header["kind"] == "plan"
    hypothesis = json.loads((world.layout.fixes_dir(world.issue_id) / "plan.json").read_text(encoding="utf-8"))[
        "hypothesis"]
    issue_root = list(context.load(world.conn, world.layout, world.issue_id).issue.root_cause)
    assert issue_root and hypothesis["cause"] and [item["location"] for item in hypothesis["evidence"]] == issue_root
    assert [item["location"] for item in hypothesis["edits"]] == issue_root
    world.runner.edits += [{TEST_FILE: TEST_CODE}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", WRITTEN, EXECUTED)
    applied = fix.apply(world.issue_id)
    assert applied.status is HandoffStatus.OK, applied.message
    assert world.runner.roles() == ["fix-executor", "fix-executor"]
    first, second = world.runner.tasks
    assert first.tests_only and first.output_schema == "runner/roles/repro-test.schema.json"
    assert "这一轮只写测试" in first.instructions.prompt and "### 验收标准" in first.instructions.prompt
    assert world.runner.resumed == [("fix-executor", None), ("fix-executor", "session-1")]
    assert "## 这一轮写代码" in second.instructions.prompt and "# 修复规则" not in second.instructions.prompt
    saved = json.loads((world.layout.fixes_dir(world.issue_id) / "repro.json").read_text(encoding="utf-8"))
    assert (saved["asExpected"], saved["checkId"], saved["outcome"]) == (True, "test-1", "passed")
    assert [item.check_id for item in regressions.find(world.conn, issue_id=world.issue_id)] == ["api-1", "test-1"]
    _, outputs = handoff(world)
    assert [item["path"] for item in outputs["changedFiles"]] == [SERVICE_PATH, TEST_FILE]
    assert outputs["rounds"][-1]["reviews"] == [] and outputs["lane"] == "fast"
    assert {item["checkId"]: item["afterFix"] for item in outputs["reproCheck"]} == {"api-1": "not-run",
                                                                                     "test-1": "passed"}
    result = documents.read(world.layout.fixes_dir(world.issue_id) / "result.md")
    assert result.header["status"] == "done" and result.blocks["checks"][0]["verdict"] == "passed"
    done = fix.done(world.issue_id)
    assert done.status is HandoffStatus.OK and world.issue().phase is IssuePhase.VERIFY
    assert progress(world).status.value == "done"
    assert [item["item"].split("：")[0] for item in progress(world).blocks["checklist"]] == [
        f"第 {step} 步 {name}" for step, name in ((0, "分流"), (1, "准备"), (5, "写复现测试"), (6, "写代码"),
                                                   (7, "收集结果"), (8, "评审"), (9, "完成"))]


def test_the_nearest_existing_test_is_given_and_existing_tests_may_not_be_edited(tmp_path):
    world = fixing(tmp_path)
    sibling = "tests/test_order_service_existing.py"
    (world.worktree / "tests").mkdir(parents=True, exist_ok=True)
    (world.worktree / sibling).write_text("from orders import service\n\ndef test_existing():\n    pass\n",
                                          encoding="utf-8")
    git = FakeGit(world.worktree, {SERVICE_PATH: SERVICE, CONTROLLER_PATH: CONTROLLER,
                                   sibling: (world.worktree / sibling).read_text(encoding="utf-8")})
    fix = service(world, git=git)
    assert fix.plan(world.issue_id).status is HandoffStatus.OK
    world.runner.edits += [{sibling: "from orders import service\n\ndef test_existing():\n    pass\n" + TEST_CODE},
                           {sibling: "from orders import service\n\ndef test_existing():\n    pass\n",
                            TEST_FILE: TEST_CODE}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", {**WRITTEN, "file": sibling, "command": f"python -m pytest -q {sibling}"},
                     WRITTEN, EXECUTED)
    assert fix.apply(world.issue_id).status is HandoffStatus.OK
    first, retry = world.runner.tasks[:2]
    assert "相邻的已有测试" in first.instructions.prompt and f"`{sibling}`" in first.instructions.prompt
    assert "from orders import service" in first.instructions.prompt
    assert f"改动了已有的测试文件 {sibling}" in retry.instructions.prompt


def test_a_small_change_in_lane_a_gets_a_light_review(tmp_path):
    world = fixing(tmp_path, outputs={"sizeTier": "small"})
    fix = service(world)
    fix.plan(world.issue_id)
    bigger = FIXED + "".join(f"guard {number}\n" for number in range(40))
    world.runner.edits += [{TEST_FILE: TEST_CODE}, {SERVICE_PATH: bigger}]
    world.runner.add("fix-executor", WRITTEN, EXECUTED).add("fix-reviewer", PASS)
    assert fix.apply(world.issue_id).status is HandoffStatus.OK
    assert world.runner.roles() == ["fix-executor", "fix-executor", "fix-reviewer-light"]
    review = world.runner.tasks[-1].instructions.prompt
    assert "## 实际结果(第 7 步)" in review and "## 特判检查" in review
    assert (world.layout.fixes_dir(world.issue_id) / "review-1-light.md").is_file()
    recorded = {item.item: item.result.value for item in scores.find(world.conn, stage=Stage.FIX)}
    assert recorded["fix.acceptance"] == "pass"


def test_a_defect_test_that_passes_on_the_base_returns_to_triage(tmp_path):
    world = fixing(tmp_path)
    fix = service(world, launcher=Launcher(passes=True))
    fix.plan(world.issue_id)
    world.runner.edits.append({TEST_FILE: TEST_CODE})
    world.runner.add("fix-executor", WRITTEN)
    result = fix.apply(world.issue_id)
    assert result.status is HandoffStatus.FAILED and "问题不成立" in result.message
    assert world.issue().status is IssueStatus.NEEDS_DECISION
    assert any(item.event is ProblemEvent.RETRIAGE_REQUESTED for item in problem_events.for_problem(world.conn,
                                                                                                    "P-0001"))


def test_tests_that_cannot_be_written_or_do_not_fail_go_to_the_user(tmp_path):
    world = fixing(tmp_path)
    fix = service(world)
    fix.plan(world.issue_id)
    world.runner.add("fix-executor", {**WRITTEN, "status": "cannot-write", "file": None, "command": None,
                                      "location": None, "reason": "缺陷只在生产数据上出现"})
    result = fix.apply(world.issue_id)
    assert result.status is HandoffStatus.FAILED and "写不出合格的复现测试" in result.message
    assert (world.issue().status, world.issue().hold.reason) == (IssueStatus.NEEDS_DECISION, "写不出合格的复现测试")
    world = fixing(tmp_path / "other", outputs={"taskType": "feature"})
    fix = service(world, launcher=Launcher(passes=True))
    fix.plan(world.issue_id)
    world.runner.edits += [{TEST_FILE: TEST_CODE}] * 3
    world.runner.add("fix-executor", WRITTEN)
    assert fix.apply(world.issue_id).status is HandoffStatus.FAILED
    assert "测试在当前代码上已经通过" in world.runner.tasks[1].instructions.prompt
    assert world.runner.resumed[1] == ("fix-executor", "session-1")


def test_lane_a_moves_to_lane_b_when_the_change_outgrows_the_tier(tmp_path):
    world = fixing(tmp_path)
    fix = service(world)
    fix.plan(world.issue_id)
    world.runner.edits += [{TEST_FILE: TEST_CODE}, {SERVICE_PATH: FIXED, CONTROLLER_PATH: CONTROLLER + "extra\n"}]
    world.runner.add("fix-executor", WRITTEN, EXECUTED)
    result = fix.apply(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and "转 B 通道" in result.message
    assert route.load(world.layout.fixes_dir(world.issue_id)).lane.value == "standard"
    assert fix.resume_point(world.issue_id) is ResumePoint.PLAN
    world.runner.add("fix-planner", plan_output(world, files=[{"path": SERVICE_PATH, "isNew": False, "reason": None},
                                                             {"path": CONTROLLER_PATH, "isNew": False,
                                                              "reason": None}]))
    assert fix.plan(world.issue_id).status is HandoffStatus.BLOCKED
    assert fix.confirm(world.issue_id).status is HandoffStatus.OK
    world.runner.add("fix-executor", EXECUTED).add("fix-reviewer", PASS)
    applied = fix.apply(world.issue_id)
    assert applied.status is HandoffStatus.OK, applied.message
    assert world.runner.roles()[2:] == ["fix-planner", "fix-executor", "fix-reviewer-light"]
    assert world.runner.resumed[3] == ("fix-executor", "session-1")


def test_lane_b_for_security_scouts_writes_the_test_apart_and_adds_a_deep_review(tmp_path):
    world = fixing(tmp_path, outputs={"taskType": "security"},
                   review={"riskRules": {"authz": {"patterns": ["filtered by company"]}}})
    fix = service(world)
    world.runner.add("fix-scout", scouting()).add("fix-planner", plan_output(world))
    planned = fix.plan(world.issue_id)
    assert planned.status is HandoffStatus.BLOCKED and planned.operation is not None
    assert (world.layout.fixes_dir(world.issue_id) / "scout.md").is_file()
    assert fix.confirm(world.issue_id).status is HandoffStatus.OK
    world.runner.edits += [{TEST_FILE: TEST_CODE}, {SERVICE_PATH: FIXED}]
    world.runner.add("repro-writer", WRITTEN).add("fix-executor", EXECUTED).add("fix-reviewer", PASS, {
        **PASS, "mode": "deep"})
    assert fix.apply(world.issue_id).status is HandoffStatus.OK
    assert world.runner.roles() == ["fix-scout", "fix-planner", "repro-writer", "fix-executor", "fix-reviewer-light",
                                    "fix-reviewer-deep"]
    assert world.runner.resumed[3] == ("fix-executor", None)
    _, outputs = handoff(world)
    assert outputs["rounds"][-1]["reviews"] == [{"mode": "light", "passed": True}, {"mode": "deep", "passed": True}]


def test_oversize_issues_ask_the_user_to_split(tmp_path):
    world = fixing(tmp_path, outputs={"sizeTier": "oversize"})
    result = service(world).plan(world.issue_id)
    assert result.status is HandoffStatus.FAILED and world.runner.tasks == []
    assert (world.issue().status, world.issue().hold.reason) == (IssueStatus.NEEDS_DECISION, "超出单个任务的上限")
    decision = documents.read(world.layout.fixes_dir(world.issue_id) / "decision.md")
    assert decision.blocks["options"][0] == {"id": "split", "summary": "把需求拆成几个各自不超过大档的 Issue，分别修复",
                                             "recommended": True}


def test_lane_c_plans_are_split_and_always_confirmed_by_the_user(tmp_path):
    world = fixing(tmp_path, outputs={"sizeTier": "large"}, gates={"issue-approve": "auto", "plan-confirm": "auto", "fix-session": "auto"})
    fix = service(world)
    later = [{"title": f"Follow up {number}", "goal": f"第 {number} 步", "files": [CONTROLLER_PATH],
              "estimate": {"files": 4, "lines": 150}, "acceptance": [f"验收 {number}"]} for number in (2, 3)]
    world.runner.add("fix-planner", plan_output(world, split={"reason": "大任务", "followUps": later},
                                                estimate={"files": 4, "lines": 150}))
    result = fix.plan(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and "大任务(C 通道)的整体方案一律交用户确认" in result.message
    assert "## 大任务：整体方案" in world.runner.tasks[0].instructions.prompt
    assert fix.confirm(world.issue_id).status is HandoffStatus.OK
    found = [record.issue for record in issues.find(world.conn) if record.issue.id != world.issue_id]
    assert [(issue.title, issue.depends_on, issue.task_type.value, issue.size_tier.value) for issue in found] == [
        ("Follow up 2", world.issue_id, "bug", "medium"), ("Follow up 3", found[0].id, "bug", "medium")]
    blocked = fix.prepare(found[0].id)
    assert blocked.status is HandoffStatus.BLOCKED and f"排在 Issue {world.issue_id} 之后" in blocked.message


def test_the_frontend_design_goes_into_the_handoff_and_the_confirmation(tmp_path):
    world = fixing(tmp_path, outputs={"sizeTier": "medium"})
    fix = service(world)
    view = "web/src/views/OrderList.vue"
    files = [{"path": SERVICE_PATH, "isNew": False, "reason": None}, {"path": view, "isNew": True, "reason": "新页面"}]
    world.runner.add("fix-planner", plan_output(world, files=files))
    world.runner.add("frontend-designer", RunnerStatus.FAILED)
    result = fix.plan(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED, result.message
    _, outputs = handoff(world)
    assert outputs["frontendDesign"] == {"files": [view], "design": None,
                                         "error": "frontend-designer：执行器返回 failed(fake-error)"}
    operation = pending_operations.get(world.conn, result.operation)
    assert "前端设计说明没有产出" in json.dumps(operation.description, ensure_ascii=False)


def standard(tmp_path, **config):
    """B 通道(中档缺陷)、计划已确认、复现测试已写好的修复。"""
    world = fixing(tmp_path, outputs={"sizeTier": "medium"}, **config)
    fix = service(world)
    world.runner.add("fix-planner", plan_output(world))
    assert fix.plan(world.issue_id).status is HandoffStatus.BLOCKED
    assert fix.confirm(world.issue_id).status is HandoffStatus.OK
    world.runner.edits.append({TEST_FILE: TEST_CODE})
    world.runner.add("fix-executor", WRITTEN)
    return world, fix


def test_local_problems_are_corrected_and_held_after_the_limit(tmp_path):
    world, fix = standard(tmp_path, checks={"residuePatterns": ["DEBUG"], **PROJECT["checks"]})
    world.runner.edits += [{SERVICE_PATH: FIXED + "DEBUG\n"}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", EXECUTED).add("fix-reviewer", PASS)
    assert fix.apply(world.issue_id).status is HandoffStatus.OK
    corrected = [task for task in world.runner.tasks if task.role == "fix-executor"][2]
    assert "[residue]" in corrected.instructions.prompt
    world, fix = standard(tmp_path / "held", checks={"residuePatterns": ["DEBUG"], **PROJECT["checks"]})
    world.runner.edits += [{SERVICE_PATH: FIXED + "DEBUG\n"}] * 4
    world.runner.add("fix-executor", EXECUTED)
    result = fix.apply(world.issue_id)
    assert result.status is HandoffStatus.FAILED and "修正与复评超过上限" in result.message
    assert (world.issue().status, world.issue().hold.reason) == (IssueStatus.NEEDS_DECISION, "修正与复评超过上限")
    assert [task.role for task in world.runner.tasks].count("fix-executor") == 5


def test_a_round_that_breaks_the_repro_test_rolls_back_to_the_checkpoint(tmp_path):
    world, fix = standard(tmp_path, checks={"residuePatterns": ["DEBUG"], **PROJECT["checks"]})
    world.runner.edits += [{SERVICE_PATH: FIXED + "DEBUG\n"}, {SERVICE_PATH: SERVICE + "DEBUG\n"}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", EXECUTED).add("fix-executor", EXECUTED).add("fix-reviewer", PASS)
    seen = []
    original = world.runner.run

    def run(task, **kwargs):
        if task.role == "fix-executor":
            seen.append((world.worktree / SERVICE_PATH).read_text(encoding="utf-8"))
        return original(task, **kwargs)

    world.runner.run = run
    assert fix.apply(world.issue_id).status is HandoffStatus.OK
    executors = [task for task in world.runner.tasks if task.role == "fix-executor"]
    assert "worktree 已恢复到第 1 轮的检查点" in executors[3].instructions.prompt
    assert seen[3] == FIXED + "DEBUG\n"


def test_oversized_changes_converge_once_and_then_go_to_the_user(tmp_path):
    oversized = FIXED + "".join(f"extra {number}\n" for number in range(510))
    world, fix = standard(tmp_path)
    world.runner.edits += [{SERVICE_PATH: oversized}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", EXECUTED).add("fix-reviewer", PASS)
    assert fix.apply(world.issue_id).status is HandoffStatus.OK
    corrected = [task for task in world.runner.tasks if task.role == "fix-executor"][2]
    assert "[size] 增删 512 行(不含测试)，超过上限 200；按计划收敛" in corrected.instructions.prompt
    world, fix = standard(tmp_path / "held")
    world.runner.edits += [{SERVICE_PATH: oversized}] * 2
    world.runner.add("fix-executor", EXECUTED)
    result = fix.apply(world.issue_id)
    assert result.status is HandoffStatus.FAILED and result.message.startswith("改动量仍超出上限：[size]")
    assert (world.issue().status, world.issue().hold.reason) == (IssueStatus.NEEDS_DECISION, "改动量仍超出上限")


def test_plan_gaps_replan_and_user_matters_stop(tmp_path):
    world, fix = standard(tmp_path)
    world.runner.edits.append({SERVICE_PATH: FIXED, CONTROLLER_PATH: CONTROLLER + "extra\n"})
    world.runner.add("fix-executor", EXECUTED).add("fix-planner", plan_output(world, summary="连同 Other 一起改"))
    result = fix.apply(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and result.operation is not None
    assert "上一次实施发现计划没覆盖" in world.runner.tasks[-1].instructions.prompt
    world, fix = standard(tmp_path / "guard")
    violation = Violation.from_dict({"kind": "protected-modified", "path": "deploy/run.sh", "detail": "受保护"})
    world.runner.run = _violating(world.runner, violation)
    result = fix.apply(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and "规范要求用户确认" in result.message
    assert world.issue().phase is IssuePhase.FIX


def _violating(runner, violation):
    original = runner.run

    def run(task, **options):
        if task.role == "fix-executor" and not task.tests_only:
            runner.tasks.append(task)
            return RunnerResult(RunnerStatus.GUARD_VIOLATION, "fake", violations=(violation,))
        return original(task, **options)

    return run


def test_design_issues_stop_and_user_decisions_are_kept(tmp_path):
    world = fixing(tmp_path, outputs={"taskType": "security"})
    issue = {"rootCause": "状态机缺一种状态", "reason": "每个入口都要补判断", "locations": [f"{SERVICE_PATH}:3"]}
    world.runner.add("fix-scout", scouting(designIssue=issue))
    fix = service(world)
    result = fix.plan(world.issue_id, "只改导出")
    assert result.status is HandoffStatus.FAILED
    assert (world.issue().status, world.issue().hold.reason) == (IssueStatus.NEEDS_DECISION, "设计问题")
    decided = decisions.load(world.layout.fixes_dir(world.issue_id))
    assert [item["text"] for item in decided.entries] == ["只改导出"]
    prompt = world.runner.tasks[0].instructions.prompt
    assert "## 用户的决定与补充" in prompt and "只改导出" in prompt
    assert fix.start(world.issue_id).status is HandoffStatus.BLOCKED
    assert fix.start(world.issue_id, here=True, force=True).status is HandoffStatus.OK
    assert world.issue().hold is None and world.issue().phase is IssuePhase.FIX


def test_unknown_review_items_wait_for_the_user(tmp_path):
    world, fix = standard(tmp_path)
    unknown = {**PASS, "items": [{"itemId": "fix.acceptance", "result": "unknown", "reason": "看不到线上数据"}]}
    world.runner.edits.append({SERVICE_PATH: FIXED})
    world.runner.add("fix-executor", EXECUTED).add("fix-reviewer", unknown)
    result = fix.apply(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and "fix confirm" in result.message
    accepted = fix.confirm(world.issue_id, note="线上确认过数据")
    assert accepted.status is HandoffStatus.OK
    _, outputs = handoff(world)
    assert outputs["unverified"][-1] == {"item": "评审给出「无法判断」的项", "reason": "用户判断：线上确认过数据"}


def test_preconditions_are_explained(tmp_path):
    world = make_fix_world(tmp_path, signal=api_signal())
    fix = service(world)
    assert fix.plan(world.issue_id).message == f"先执行 fix start {world.issue_id}"
    shutil.rmtree(world.worktree)
    assert fix.start(world.issue_id).message == f"先执行 fix prepare {world.issue_id}"
    world = fixing(tmp_path / "fixing", outputs={"sizeTier": "medium"})
    fix = service(world)
    world.runner.add("fix-planner", plan_output(world))
    assert fix.resume_point(world.issue_id) is ResumePoint.PLAN
    fix.plan(world.issue_id)
    assert fix.resume_point(world.issue_id) is ResumePoint.CONFIRM
    assert fix.apply(world.issue_id).message == "计划已变化或尚未确认，请重新确认"
    fix.confirm(world.issue_id)
    assert fix.resume_point(world.issue_id) is ResumePoint.APPLY
    assert fix.done(world.issue_id).message == f"最近一次 fix apply 没有通过，先执行 fix apply {world.issue_id}"


def test_done_checks_the_diff_hash_and_abandon_holds(tmp_path):
    world, fix = standard(tmp_path)
    world.runner.edits.append({SERVICE_PATH: FIXED})
    world.runner.add("fix-executor", EXECUTED).add("fix-reviewer", PASS)
    fix.apply(world.issue_id)
    (world.worktree / SERVICE_PATH).write_text(FIXED + "more\n", encoding="utf-8")
    assert "--review-only" in fix.done(world.issue_id).message
    assert fix.abandon(world.issue_id, "改为找作者讨论").status is HandoffStatus.OK
    assert world.issue().hold.details == "改为找作者讨论"


def test_prepare_checks_the_base_and_the_start_session(tmp_path):
    world = make_fix_world(tmp_path, signal=api_signal(), checks={"prepare": [
        {"name": "deps", "cwd": ".", "command": "make deps"}], **PROJECT["checks"]})

    class Planner:
        def plan_create_fix_worktree(self, issue_id, branch):
            self.branch = branch
            return _operation(issue_id, branch)

    planner = Planner()
    launcher = Launcher(world.worktree)
    fix = service(world, planner=planner, launcher=launcher)
    result = fix.prepare(world.issue_id)
    expected = f"bugfix/{int(world.issue().id)}-{world.issue().slug}"
    assert result.status is HandoffStatus.BLOCKED and planner.branch == expected
    fix.on_executed(_operation(world.issue_id, planner.branch))
    assert world.issue().branch == planner.branch and world.issue().hold is None
    saved = json.loads((world.layout.fixes_dir(world.issue_id) / "workspace.json").read_text())
    assert (saved["baseCommit"], saved["baseChecks"]) == (BASE, "passed")
    assert [command.argv for command in launcher.commands] == [("make", "deps"), ("python", "-m", "pytest", "-q")]
    started = fix.start(world.issue_id)
    assert started.status is HandoffStatus.OK and world.issue().phase is IssuePhase.FIX
    kind, task, first = world.runner.sessions[-1]
    assert kind == "new" and task.interactive and task.access.value == "read-only"
    assert "tightrein fix" in task.allowed_commands and "当前停在：fix plan" in first
    service(world).start(world.issue_id)
    assert [item[0] for item in world.runner.sessions[-2:]] == ["resume", "new"]
    broken = make_fix_world(tmp_path / "broken", signal=api_signal(), **PROJECT)
    service(broken, launcher=Launcher(broken.worktree, base_fails=True)).on_executed(
        _operation(broken.issue_id, "cty/fix-x"))
    assert broken.issue().hold.reason == "项目检查在基准版本上不通过(配置错误)"


def _operation(issue_id, branch):
    from datetime import datetime, timezone

    from tightrein.vcs.operations import OperationDescription

    return PendingOperation(
        id="OP-0009", stage=Stage.FIX, subject_id=issue_id, kind=OperationKind.CREATE_FIX_WORKTREE,
        executor=OperationExecutor.VCS,
        commands=(), description=OperationDescription("/repo", branch, (), False, "撤销", "新建"), impact="本地",
        reversible=True, preconditions={"target": {"branch": branch}, "base": BASE}, idempotency_key="k",
        confirmations_required=1, created_at=datetime(2026, 10, 5, tzinfo=timezone.utc))


def test_output_mode_writes_the_patch_without_touching_the_database(tmp_path):
    world = fixing(tmp_path)
    output = tmp_path / "out"
    layout = WorkspaceLayout(world.layout.root, output)
    fix = service(world, layout=layout)
    assert fix.plan(world.issue_id).status is HandoffStatus.OK
    assert not pending_operations.find(world.conn, subject_id=world.issue_id)
    world.runner.edits += [{TEST_FILE: TEST_CODE}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", WRITTEN, EXECUTED)
    applied = fix.apply(world.issue_id)
    assert applied.status is HandoffStatus.OK, applied.message
    assert (output / "changes.patch").read_text(encoding="utf-8").startswith(f"diff --git a/{SERVICE_PATH}")
    assert (output / "handoff" / f"fix-{world.issue_id}.json").is_file()
    assert (output / "regressions" / world.issue_id / "check.yaml").is_file()
    assert scores.find(world.conn, stage=Stage.FIX) == [] and world.issue().phase is IssuePhase.FIX


def test_a_manual_issue_takes_lane_b_and_its_feature_test_is_registered(tmp_path):
    requirement = "订单列表按日期筛选。\n\n## 验收标准\n\n- 选择日期后只显示当天的订单\n"
    world = fixing(tmp_path, manual=("按日期筛选订单", requirement))
    fix = service(world)
    world.runner.add("fix-scout", scouting()).add("fix-planner", plan_output(world))
    result = fix.plan(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and result.operation is not None, result.message
    found = route.load(world.layout.fixes_dir(world.issue_id))
    assert (found.lane.value, found.task_type.value, found.tier.value) == ("standard", "feature", "micro")
    scout_prompt = world.runner.tasks[0].instructions.prompt
    assert "用户直接提出的需求" in scout_prompt and "订单列表按日期筛选" in scout_prompt
    assert not world.layout.regression_dir(world.issue_id).exists()
    assert fix.confirm(world.issue_id).status is HandoffStatus.OK
    world.runner.edits += [{TEST_FILE: TEST_CODE}, {SERVICE_PATH: FIXED}]
    world.runner.add("fix-executor", WRITTEN, EXECUTED).add("fix-reviewer", PASS)
    assert fix.apply(world.issue_id).status is HandoffStatus.OK
    assert manifest.load(world.layout.regression_dir(world.issue_id)).problems == ()


AUTONOMY = {"gates": {"issue-approve": "auto", "plan-confirm": "auto", "fix-session": "auto"}}


def issue_text(world):
    return (world.layout.root / issues.get(world.conn, world.issue_id).path).read_text(encoding="utf-8")


def test_prepare_runs_directly_when_the_project_skips_confirmation(tmp_path):
    world = make_fix_world(tmp_path, signal=api_signal(), gates={"release-writes": "auto"})
    branch = f"bugfix/{int(world.issue().id)}-{world.issue().slug}"

    class Planner:
        def plan_create_fix_worktree(self, issue_id, name):
            return _operation(issue_id, name)

    class Operations:
        def run_unattended(self, operation_id, *, reason, clock):
            self.reason = reason
            executed = replace(_operation(world.issue_id, branch), status=OperationStatus.EXECUTED)
            fix.on_executed(executed)
            return OperationResult(executed, ran=True)

    operations = Operations()
    fix = service(world, planner=Planner(), operations=operations)
    result = fix.prepare(world.issue_id)
    assert result.status is HandoffStatus.OK and result.operation is None and "已直接建立修复分支" in result.message
    assert world.issue().branch == branch and "gates.release-writes" in operations.reason


def test_autonomy_confirms_a_simple_plan(tmp_path):
    world = fixing(tmp_path, outputs={"sizeTier": "medium"}, **AUTONOMY)
    fix = service(world)
    world.runner.add("fix-planner", plan_output(world))
    decided = fix.plan(world.issue_id)
    assert decided.status is HandoffStatus.OK and "自动确认修复计划：满足" in decided.message
    assert fix.resume_point(world.issue_id) is ResumePoint.APPLY
    assert "预估改动 1 个文件、2 行" in issue_text(world)
    assert fix.auto_confirm(world.issue_id) is None


def test_autonomy_leaves_plans_needing_decisions_to_the_user(tmp_path):
    world = fixing(tmp_path, outputs={"sizeTier": "medium"}, **AUTONOMY)
    fix = service(world)
    asked = [{"question": "是否保留旧接口", "recommendation": "保留", "reason": "兼容"}]
    world.runner.add("fix-planner", plan_output(world, userDecisions=asked, estimate={"files": 4, "lines": 20}))
    result = fix.plan(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and result.operation is not None
    assert "修复计划需要用户确认：待定：是否保留旧接口" in result.message
    assert "预估改动 4 个文件、20 行，超过 3 个文件、100 行" in result.message
    assert fix.auto_confirm(world.issue_id).status is HandoffStatus.BLOCKED
    assert issue_text(world).count("修复计划需要用户确认") == 1
    assert fix.resume_point(world.issue_id) is ResumePoint.CONFIRM
