import json

from fix_world import CONTROLLER_PATH, SERVICE_PATH, make_fix_world

from tightrein.domain.enums import FixRiskLevel, OperationStatus, ReviewMode, RunnerStatus
from tightrein.pipeline.fix.prompts import fix_reviewer
from tightrein.pipeline.fix.prompts.common import FixCalls, FixPrompt
from tightrein.pipeline.fix.render import documents
from tightrein.store.files import documents as document_files
from tightrein.pipeline.fix.steps import context, plan, plan_gate
from tightrein.pipeline.fix.steps import risk as risk_step
from tightrein.pipeline.fix.steps.plan import ProposalSettings
from tightrein.store.files.layout import ToolLayout
from tightrein.store.repos import pending_operations

RUN = "R-20261005-030000-fix"
FLAGS = {"design": {"flagged": False}, "dataStructure": {"flagged": False}, "publicContract": {"flagged": False}}


def scouting(**changes):
    output = {"analysis": "Get 按编号查询，控制器直接调用。",
              "existing": [{"location": f"{SERVICE_PATH}:12", "description": "按编号查询，没有校验归属"}],
              "reusable": [], "dataStructure": [],
              "linkage": [{"location": f"{CONTROLLER_PATH}:8", "description": "控制器调用 Get"}], "problems": [],
              "designIssue": None, "affectedEndpoints": ["GET /api/Order/{id}"], "affectedPages": [],
              "incidental": []}
    output.update(changes)
    return output


def fix_plan(ctx, **changes):
    output = {
        "analysis": "根因在 Get 没有按公司过滤，只改这一处即可。",
        "summary": "在 OrderService.Get 中按公司过滤",
        "hypothesis": {"cause": "其他公司的用户请求订单 → Get 只按编号查询 → 返回了不属于该公司的订单",
                       "evidence": [{"location": f"{SERVICE_PATH}:12", "fact": "查询条件只有编号"}],
                       "edits": [{"location": f"{SERVICE_PATH}:12", "change": "加公司过滤条件"}]},
        "steps": [{"file": SERVICE_PATH, "change": "加过滤条件",
                                                                 "verification": "运行受影响的测试"}],
        "files": [{"path": SERVICE_PATH, "isNew": False, "reason": None}], "estimate": {"files": 1, "lines": 4},
        "split": None, "protectedTouches": [], "flags": FLAGS, "migration": None, "newDependencies": [],
        "deletions": [], "acceptanceMapping": [{"criterion": item, "steps": [1]} for item in ctx.acceptance],
        "userVisibleChange": "无", "affectedEndpoints": ["GET /api/Order/{id}"], "affectedPages": [],
        "notDoing": ["不调整其他查询"], "userDecisions": [],
    }
    output.update(changes)
    return output


def setup(tmp_path, **config):
    world = make_fix_world(tmp_path, **config)
    ctx = context.load(world.conn, world.layout, world.issue_id)
    calls = FixCalls(world.runner, world.clock, FixPrompt(ToolLayout(), world.config, RUN, world.worktree))
    return world, ctx, calls


def settings(world, **changes):
    values = dict(worktree=world.worktree, config=world.config, protected=("src/Auth/",), max_files=5,
                  max_lines=150, rounds=2, scout=True)
    values.update(changes)
    return ProposalSettings(**values)


def test_scouting_locations_are_checked_and_fed_back(tmp_path):
    world, ctx, calls = setup(tmp_path)
    world.runner.add("fix-scout", scouting(existing=[{"location": f"{SERVICE_PATH}:99", "description": "x"}]),
                     scouting())
    world.runner.add("fix-planner", fix_plan(ctx))
    proposal = plan.propose(calls, ctx, settings(world))
    assert proposal.plan["summary"] == "在 OrderService.Get 中按公司过滤" and proposal.risk.level is FixRiskLevel.NORMAL
    assert world.runner.roles() == ["fix-scout", "fix-scout", "fix-planner"]
    assert f"{SERVICE_PATH} 只有 40 行，引用了第 99 行" in world.runner.tasks[1].instructions.prompt
    assert proposal.plan["risk"] == {"level": "normal", "categories": [], "hits": []}


def test_planning_without_scouting_and_the_large_task_note(tmp_path):
    world, ctx, calls = setup(tmp_path, review={"riskRules": {"authz": {"paths": ["src/Services/"]}}})
    world.runner.add("fix-planner", fix_plan(ctx))
    proposal = plan.propose(calls, ctx, settings(world, scout=False, large=True))
    assert world.runner.roles() == ["fix-planner"] and proposal.scouting is None
    assert proposal.plan["risk"]["hits"][0]["file"] == SERVICE_PATH
    prompt = world.runner.tasks[0].instructions.prompt
    assert "## 大任务：整体方案" in prompt and "## 勘察结论" not in prompt


def test_the_second_judgement_uses_the_actual_changes(tmp_path):
    from tightrein.guards.diff_rules import ChangedFile

    world, ctx, calls = setup(tmp_path, review={"riskRules": {"schema": {"patterns": ["create_table("]}}})
    changes = [ChangedFile(SERVICE_PATH, added=("create_table(audit)",))]
    judged = risk_step.apply_risk(changes, fix_plan(ctx), ctx, world.config, set())
    assert judged.level is FixRiskLevel.HIGH and judged.hits[0].basis == "content"
    planned = fix_plan(ctx, migration={"entries": ["0042"], "reversible": True, "revertMethod": "回滚 0042"})
    assert risk_step.apply_risk([], planned, ctx, world.config, set()).hits[0].basis == "migration"
    endpoints = risk_step.endpoint_files({"endpoints": [{"sourceFile": SERVICE_PATH}, {"sourceFile": None}]})
    assert endpoints == {SERVICE_PATH}


def test_plan_checks(tmp_path):
    world, ctx, calls = setup(tmp_path)

    def problems(**changes):
        return plan.check(fix_plan(ctx, **changes), ctx, world.worktree, ("src/Auth/",), 5, 150, ("tests/",)).problems

    assert problems() == ()
    # 换掉文件清单时根因假说的对应关系也会报问题，这里只看文件本身的那一条
    assert problems(files=[{"path": "src/Missing.src", "isNew": False, "reason": None}])[0] == (
        "计划中的已有文件 src/Missing.src 不存在")
    assert problems(files=[{"path": SERVICE_PATH, "isNew": False, "reason": None},
                           {"path": "src/Auth/Matrix.src", "isNew": True, "reason": "新建"}]) == (
        "src/Auth/Matrix.src 是受保护文件，须写进 protectedTouches 并说明改什么、为什么",)
    assert "超出上限 5 个文件、150 行(不含测试)：拆分" in problems(estimate={"files": 6, "lines": 10})[0]
    later = {"title": "清理旧调用", "goal": "删去旧接口的调用方", "files": ["src/A.src"],
             "estimate": {"files": 1, "lines": 10}, "acceptance": ["没有旧接口的调用"]}
    assert problems(split={"reason": "超出上限", "followUps": [later]}) == ()
    assert problems(split={"reason": "超出上限", "followUps": [{**later, "estimate": {"files": 1, "lines": 151}}]}) == (
        "拆分的第 2 个子任务「清理旧调用」预估改动 1 个文件、151 行，超出上限 5 个文件、150 行(不含测试)：拆分为有先后顺序、"
        "各自单独成立的子任务，本计划只做第一个，其余写进 split",)
    assert "没有出现在 acceptanceMapping 中" in problems(acceptanceMapping=[])[0]
    design = dict(FLAGS, design={"flagged": True, "reason": "状态机缺一种状态", "locations": [f"{SERVICE_PATH}:3"]})
    assert plan.check(fix_plan(ctx, flags=design), ctx, world.worktree, (), 5, 150, ()).design


def test_the_root_cause_hypothesis_is_checked_against_the_code_and_the_files(tmp_path):
    world, ctx, calls = setup(tmp_path)

    def problems(**changes):
        planned = fix_plan(ctx)
        planned["hypothesis"] = {**planned["hypothesis"], **changes.pop("hypothesis", {})}
        planned.update(changes)
        return plan.check(planned, ctx, world.worktree, (), 5, 150, ("tests/",)).problems

    planned_edit = {"location": f"{SERVICE_PATH}:12", "change": "加公司过滤条件"}
    assert problems(hypothesis={"evidence": [{"location": f"{SERVICE_PATH}:99", "fact": "越界"}]}) == (
        f"根因假说中的位置不存在或越界：{SERVICE_PATH} 只有 40 行，引用了第 99 行",)
    assert problems(hypothesis={"edits": [planned_edit, {"location": f"{CONTROLLER_PATH}:8", "change": "改"}]}) == (
        f"修改位置 {CONTROLLER_PATH}:8 的文件不在 files 中",)
    controller = {"path": CONTROLLER_PATH, "isNew": False, "reason": "联动"}
    service = {"path": SERVICE_PATH, "isNew": False, "reason": None}
    assert problems(files=[service, controller]) == (
        f"计划修改 {CONTROLLER_PATH} 但根因假说没有给出修改位置(hypothesis.edits)",)
    test_file = {"path": "tests/test_order.src", "isNew": True, "reason": "复现"}
    assert problems(files=[service, test_file]) == ()
    assert problems(hypothesis={"edits": []}, files=[{"path": "src/New.src", "isNew": True, "reason": "新模块"}]) == ()


def test_replanning_stops_at_the_limit_and_design_issues_are_not_replanned(tmp_path):
    world, ctx, calls = setup(tmp_path)
    world.runner.add("fix-scout", scouting()).add("fix-planner", RunnerStatus.SCHEMA_INVALID, fix_plan(ctx, files=[
        {"path": "src/Missing.src", "isNew": False, "reason": None}]))
    proposal = plan.propose(calls, ctx, settings(world, rounds=1))
    assert proposal.plan is None and world.runner.roles() == ["fix-scout", "fix-planner", "fix-planner"]
    assert proposal.problems[:2] == ["fix-planner：执行器返回 schema-invalid(fake-error)",
                                     "计划中的已有文件 src/Missing.src 不存在"]
    world, ctx, calls = setup(tmp_path / "design")
    issue = {"rootCause": "状态机缺一种状态", "reason": "每个入口都要补判断", "locations": [f"{SERVICE_PATH}:3"]}
    world.runner.add("fix-scout", scouting(designIssue=issue))
    proposal = plan.propose(calls, ctx, settings(world))
    assert proposal.design == issue and world.runner.roles() == ["fix-scout"]


def test_an_accepted_design_issue_is_planned(tmp_path):
    world, ctx, calls = setup(tmp_path)
    issue = {"rootCause": "状态机缺一种状态", "reason": "每个入口都要补判断", "locations": [f"{SERVICE_PATH}:3"]}
    world.runner.add("fix-scout", scouting(designIssue=issue)).add("fix-planner", fix_plan(ctx))
    proposal = plan.propose(calls, ctx, settings(world, design_accepted=True))
    assert proposal.design is None and proposal.plan is not None
    assert world.runner.roles() == ["fix-scout", "fix-planner"]


def test_user_decisions_reach_every_role(tmp_path):
    from tightrein.pipeline.fix.prompts import fix_executor, frontend_designer, repro_test

    world, ctx, calls = setup(tmp_path)
    ctx.decisions = "- 2026-10-05T03:00:00Z(fix plan --note)：只改导出，不动存储格式"
    world.runner.add("fix-scout", scouting()).add("fix-planner", fix_plan(ctx))
    plan.propose(calls, ctx, settings(world))
    prompts = [task.instructions.prompt for task in world.runner.tasks] + [
        repro_test.task(calls.prompt, ctx, "任务", 1, role="fix-executor", expects_pass=False, test_paths=(),
                        prefixes=(), check_commands=()).instructions.prompt,
        frontend_designer.task(calls.prompt, ctx, fix_plan(ctx), ["web/a.vue"], 1).instructions.prompt,
        fix_executor.task(calls.prompt, ctx, fix_plan(ctx), 1, check_commands=(), approved_protected=(),
                          budget=None).instructions.prompt]
    assert all("## 用户的决定与补充" in text and "只改导出，不动存储格式" in text for text in prompts)


def test_plan_files_confirmation_and_rendering(tmp_path):
    world, ctx, calls = setup(tmp_path)
    directory = world.layout.fixes_dir(world.issue_id)
    planned = fix_plan(ctx, protectedTouches=[{"path": "src/Auth/Matrix.src", "change": "加一行", "reason": "授权"}],
                       risk={"level": "normal", "categories": [], "hits": []})
    writer = documents.Writer(directory, world.issue_id, "zh")
    from tightrein.domain.enums import DocumentStatus
    from tightrein.pipeline.fix.render import plan as plan_render

    document = documents.plan(writer, world.clock.now(), planned, status=DocumentStatus.PENDING,
                              attention=plan_render.attention(planned))
    text = document_files.render(document, "zh")
    assert document_files.check(text) == []
    assert "受保护文件 src/Auth/Matrix.src：加一行(理由：授权)" in text and "### 文件与步骤" in text
    assert "根因假说：其他公司的用户请求订单" in text and f"- {SERVICE_PATH}:12：查询条件只有编号" in text
    assert f"修改位置：\n- {SERVICE_PATH}:12：加公司过滤条件" in text
    older = {key: value for key, value in planned.items() if key != "hypothesis"}
    older_text = document_files.render(documents.plan(writer, world.clock.now(), older, status=DocumentStatus.PENDING,
                                                      attention=[]), "zh")
    assert document_files.check(older_text) == [] and "根因假说" not in older_text
    path = plan_gate.save(directory, planned, text)
    first = plan_gate.request(world.conn, world.clock, repo="/repo", issue_id=world.issue_id,
                              plan_sha=plan_gate.sha256(path), text="确认计划")
    plan_gate.save(directory, {**planned, "summary": "改为在入口过滤"}, text)
    assert (directory / "plan.1.json").is_file()
    second = plan_gate.request(world.conn, world.clock, repo="/repo", issue_id=world.issue_id,
                               plan_sha=plan_gate.sha256(path), text="确认计划")
    assert pending_operations.get(world.conn, first.id).status is OperationStatus.EXPIRED
    assert second.preconditions == {"planSha256": plan_gate.sha256(path)}
    assert plan_gate.confirmed(directory) is None
    plan_gate.record(directory, second.id, plan_gate.sha256(path), world.clock)
    assert plan_gate.confirmed(directory)["operationId"] == second.id
    path.write_text(json.dumps({**planned, "summary": "又改了"}), encoding="utf-8")
    assert plan_gate.confirmed(directory) is None


def test_prompts_carry_the_rules_and_the_deep_review_hides_the_executor(tmp_path):
    world, ctx, calls = setup(tmp_path)
    world.runner.add("fix-scout", scouting()).add("fix-planner", fix_plan(ctx))
    plan.propose(calls, ctx, settings(world))
    planner = world.runner.tasks[1].instructions.prompt
    assert "# 修复规则" in planner and "## 验收标准" in planner and "改动文件不超过 5 个" in planner
    assert "- 改动满足 Issue 的验收标准，根因被修掉，没有破坏已有调用方" in planner
    assert "[fix.acceptance]" not in planner and "评分表" not in planner
    assert "hypothesis 必填" in planner and "13. **根因假说**" in planner
    review = fix_reviewer.task(calls.prompt, ctx, fix_plan(ctx), "+x", [], ReviewMode.DEEP, 1)
    assert (review.role, review.route, review.access.value) == ("fix-reviewer-deep", "fix.review.deep", "read-only")
    # 深度评审盲审：看不到计划(含根因假说)与 Issue 正文，只有验收标准与 diff
    deep = review.instructions.prompt
    assert "(盲审)" in deep and "## 已确认的修复计划" not in deep and "Get 只按编号查询" not in deep
    assert ctx.issue.title not in deep and "deviations" not in deep
    light = fix_reviewer.task(calls.prompt, ctx, fix_plan(ctx), "+x", [], ReviewMode.LIGHT, 1)
    assert light.route == "fix.review.light" and "## 已确认的修复计划" in light.instructions.prompt


VIEW_PATH = "web/src/views/OrderList.vue"
DESIGN = {"pages": [{"location": VIEW_PATH, "structure": "筛选栏 + 表格 + 分页"}], "layout": ["筛选栏在表格上方"],
          "interactions": [{"target": "删除按钮", "behavior": "二次确认后删除"}],
          "states": [{"target": "订单表格", "loading": "表格骨架", "empty": "暂无订单与新建入口", "error": "错误提示与重试按钮"}],
          "styling": [{"target": "表格行间距", "value": "var(--space-2)", "source": "--space-2"}],
          "mobile": ["窄屏下筛选栏折叠"], "copy": [{"location": "空状态", "text": "暂无订单", "key": "order.empty"}],
          "planConflicts": []}


def frontend_plan(ctx):
    return fix_plan(ctx, files=[{"path": SERVICE_PATH, "isNew": False, "reason": None},
                                {"path": VIEW_PATH, "isNew": True, "reason": "新的列表页"}],
                    estimate={"files": 2, "lines": 40})


def test_plans_with_frontend_files_get_a_frontend_design(tmp_path):
    from tightrein.pipeline.fix.prompts import fix_executor

    world, ctx, calls = setup(tmp_path)
    world.runner.add("fix-scout", scouting()).add("fix-planner", frontend_plan(ctx)).add("frontend-designer", DESIGN)
    proposal = plan.propose(calls, ctx, settings(world))
    assert world.runner.roles() == ["fix-scout", "fix-planner", "frontend-designer"]
    designer = world.runner.tasks[2]
    assert (designer.route, designer.conditions, designer.readonly) == ("fix.frontend-designer", (), True)
    assert f"- `{VIEW_PATH}`" in designer.instructions.prompt and SERVICE_PATH not in designer.instructions.prompt.split(
        "## 计划中的前端文件")[1].split("##")[0]
    assert proposal.plan["frontendDesign"] == DESIGN
    assert proposal.frontend == {"files": [VIEW_PATH], "design": DESIGN, "error": None}
    executor = fix_executor.task(calls.prompt, ctx, proposal.plan, 1, check_commands=(), approved_protected=(),
                                 budget=None)
    assert fix_executor.DESIGN_NOTE in executor.instructions.prompt
    from tightrein.domain.enums import DocumentStatus

    writer = documents.Writer(world.layout.fixes_dir(world.issue_id), world.issue_id, "zh")
    rendered = document_files.render(documents.plan(writer, world.clock.now(), proposal.plan,
                                                    status=DocumentStatus.PENDING, attention=[]), "zh")
    assert "前端设计说明" in rendered and "筛选栏在表格上方" in rendered


def test_backend_only_plans_skip_the_designer_and_failures_do_not_block(tmp_path):
    world, ctx, calls = setup(tmp_path)
    world.runner.add("fix-scout", scouting()).add("fix-planner", fix_plan(ctx))
    proposal = plan.propose(calls, ctx, settings(world))
    assert world.runner.roles() == ["fix-scout", "fix-planner"] and proposal.frontend is None
    assert "frontendDesign" not in proposal.plan
    world, ctx, calls = setup(tmp_path / "failed")
    world.runner.add("fix-scout", scouting()).add("fix-planner", frontend_plan(ctx))
    world.runner.add("frontend-designer", RunnerStatus.FAILED)
    proposal = plan.propose(calls, ctx, settings(world))
    assert proposal.plan is not None and "frontendDesign" not in proposal.plan
    assert proposal.frontend["error"] == "frontend-designer：执行器返回 failed(fake-error)"


def test_conditions_follow_the_risk_lane_and_frontend_root_causes(tmp_path):
    from tightrein.pipeline.fix.prompts import fix_executor

    world, ctx, calls = setup(tmp_path, stages={"fix": {"roles": {"frontend-designer": {"paths": ["src/Services/"]}}}})
    world.runner.add("fix-scout", scouting()).add("fix-planner", fix_plan(ctx)).add("frontend-designer", DESIGN)
    plan.propose(calls, ctx, settings(world, large=True))
    scout, planner, _ = world.runner.tasks
    assert (scout.route, scout.conditions) == ("fix.scout", ("frontend",))
    assert (planner.route, planner.conditions) == ("fix.planner", ("large",))
    risky = {**fix_plan(ctx), "risk": {"level": FixRiskLevel.HIGH.value, "categories": [], "hits": []}}
    executor = fix_executor.task(calls.prompt, ctx, risky, 1, check_commands=(), approved_protected=(), budget=None)
    assert (executor.route, executor.conditions) == ("fix.executor", ("high-risk",))
    plain = fix_executor.task(calls.prompt, ctx, fix_plan(ctx), 1, check_commands=(), approved_protected=(),
                              budget=None)
    assert plain.conditions == ()
    from tightrein.domain.fix import FixRisk
    from tightrein.pipeline.fix.prompts.fix_planner import PlanInputs, conditions
    assert conditions(PlanInputs(None, FixRisk(FixRiskLevel.HIGH), (), 1, 1, large=True)) == ("high-risk", "large")
