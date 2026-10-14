"""方案：程序核对(根因假说、文件、受保护文件、改动量、验收标准)，设计问题停在关卡，放不下退回评估，重出时带原因。"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from tightrein.agents.params import HIGH_RISK
from tightrein.agents.result import CallResult, CallStatus
from tightrein.assess.notes import CodeNotes
from tightrein.implement.context import Decision
from tightrein.implement.design import design, frontend
from tightrein.implement.prompts.design import DESIGN_ACCEPTED
from tightrein.protocol.handoff import Status, load_schema, schema_errors
from tightrein.protocol.naming import format_iso
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

CRITERIA = ["列表显示最近 7 天的订单", "接口 GET /api/orders 返回 200"]


def _plan(**changes: Any) -> dict[str, Any]:
    flag = {"flagged": False, "reason": None}
    found: dict[str, Any] = {
        "knowledgeSuggestions": ["日期范围统一用 recent()"], "analysis": "recent 只返回当天",
        "summary": "查询改为最近 7 天",
        "hypothesis": {"cause": "days 未参与查询 → recent 只返回当天 → 列表只有当天",
                       "evidence": [{"location": "src/orders.py:2", "fact": "return days"}],
                       "edits": [{"location": "src/orders.py:2", "change": "按 days 计算起始日"}]},
        "steps": [{"file": "src/orders.py", "change": "recent 按 days 过滤", "verification": "pytest"}],
        "files": [{"path": "src/orders.py", "isNew": False, "reason": None}],
        "estimate": {"files": 1, "lines": 4}, "oversize": None, "blockedBy": None, "protectedTouches": [],
        "flags": {"design": dict(flag), "dataStructure": dict(flag), "publicContract": dict(flag)},
        "migration": None, "newDependencies": [], "deletions": [],
        "acceptanceMapping": [{"criterion": item, "steps": [1]} for item in CRITERIA],
        "userVisibleChange": "订单列表显示 7 天", "affectedEndpoints": ["GET /api/orders"], "affectedPages": [],
        "notDoing": ["不改分页"], "userDecisions": []}
    found.update(changes)
    return found


class FakeAsk:
    """按调用点依次给出预置结果(None 表示调用失败)；输出先按各自的 schema 校验。"""

    def __init__(self, **outputs: list[dict[str, Any] | None]) -> None:
        self.outputs = {point: list(items) for point, items in outputs.items()}
        self.requests: list[Any] = []

    def __call__(self, runtime: Any, context: Any, request: Any, usage: Any) -> CallResult:
        self.requests.append(request)
        output = self.outputs[request.point.replace(".", "_")].pop(0)
        if output is None:
            return CallResult(CallStatus.FAILED, "claude", "opus", error="出错")
        assert schema_errors(output, load_schema(request.schema)) == []
        return CallResult(CallStatus.OK, "claude", "opus", output=output)


def _install(monkeypatch: pytest.MonkeyPatch, *plans: dict[str, Any] | None,
             frontend_outputs: list[dict[str, Any] | None] | None = None) -> FakeAsk:
    fake = FakeAsk(implement_design=list(plans), implement_design_frontend=frontend_outputs or [])
    monkeypatch.setattr(design, "ask", fake)
    monkeypatch.setattr(frontend, "ask", fake)
    return fake


def test_the_plan_checks(world: Any) -> None:
    settings, root = world.runtime.settings, world.repo.path
    assert design.check(_plan(), CRITERIA, root, settings) == []
    bad = _plan(files=[{"path": "src/orders.py", "isNew": False, "reason": None},
                       {"path": "src/gone.py", "isNew": False, "reason": None},
                       {"path": ".env", "isNew": True, "reason": "配置"},
                       {"path": "package.json", "isNew": True, "reason": "依赖"}],
                estimate={"files": 11, "lines": 30})
    problems = design.check(bad, CRITERIA, root, settings)
    assert "方案中的已有文件 src/gone.py 不存在" in problems
    assert ".env 是禁改文件，不能出现在方案里" in problems
    assert any(item.startswith("package.json 是高风险文件") for item in problems)
    assert any(item.startswith("预估改动 11 个文件") for item in problems)
    assert "方案要改 src/gone.py，但根因假说没有给出修改位置(hypothesis.edits)" in problems
    touched = _plan(files=bad["files"][:1] + bad["files"][3:],
                    protectedTouches=[{"path": "package.json", "change": "加脚本", "reason": "测试要用"}])
    assert design.check(touched, CRITERIA, root, settings) == []


def test_the_root_cause_hypothesis_is_checked_against_the_code_and_the_files(world: Any) -> None:
    settings, root = world.runtime.settings, world.repo.path
    plan = _plan()
    plan["hypothesis"]["evidence"] = [{"location": "src/orders.py:20", "fact": "臆造的行"}]
    plan["hypothesis"]["edits"] = [{"location": "src/users.py:1", "change": "顺手改"}]
    problems = design.hypothesis_problems(plan, root, settings)
    assert problems == ["根因假说中的位置不存在或越界：src/orders.py 只有 2 行，引用了第 20 行",
                        "修改位置 src/users.py:1 的文件不在 files 中",
                        "方案要改 src/orders.py，但根因假说没有给出修改位置(hypothesis.edits)"]
    # 测试文件与只新建的文件不要求修改位置
    tests_only = _plan(files=[{"path": "src/orders.py", "isNew": False, "reason": None},
                              {"path": "tests/test_orders.py", "isNew": False, "reason": None},
                              {"path": "src/new.py", "isNew": True, "reason": "没有合适的落点"}])
    assert design.hypothesis_problems(tests_only, root, settings) == []


def test_every_step_maps_to_an_acceptance_criterion(world: Any) -> None:
    plan = _plan(steps=_plan()["steps"] * 2, acceptanceMapping=[{"criterion": CRITERIA[0], "steps": [1, 3]}])
    problems = design.check(plan, CRITERIA, world.repo.path, world.runtime.settings)
    assert f"验收标准「{CRITERIA[1]}」没有出现在 acceptanceMapping 中" in problems
    assert "acceptanceMapping 引用了不存在的第 3 步" in problems
    assert any(item.startswith("第 2 步「recent 按 days 过滤」没有对应任何验收标准") for item in problems)


def test_a_passing_plan_is_recorded_with_its_hash(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _plan(estimate={"files": 99, "lines": 4}), _plan())
    result = design.run(world.runtime, world.context())
    assert result.status is Status.PASSED and len(fake.requests) == 2
    assert fake.requests[1].variables["feedback"].startswith("- 预估改动 99 个文件")
    facts = result.facts
    assert "analysis" not in facts and result.notes == "recent 只返回当天"  # 分析只放备注
    assert facts["version"] == 1 and facts["acceptance"] == CRITERIA
    assert facts["planHash"] == design.plan_hash(facts) and len(facts["planHash"]) == 64
    assert facts["risk"] == {"high": False, "reasons": [], "highRiskPaths": []} and facts["highRiskPaths"] == []
    assert facts["knowledgeSuggestions"] == ["日期范围统一用 recent()"]
    assert facts["frontendDesign"] is None and facts["frontendFiles"] == []
    assert fake.requests[0].conditions == ()


def test_high_risk_notes_choose_the_high_risk_model(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _plan())
    context = world.context()
    context.notes = CodeNotes("0018", world.repo.base, files=["migrations/001_orders.sql"])
    design.run(world.runtime, context)
    assert fake.requests[0].conditions == (HIGH_RISK,)
    assert "migrations/001_orders.sql 是高风险文件" in fake.requests[0].variables["risk"]


def test_design_issues_stop_unless_the_user_accepted(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    flagged = _plan(flags={**_plan()["flags"], "design": {"flagged": True, "reason": "日期过滤散落在各处"}})
    _install(monkeypatch, flagged)
    result = design.run(world.runtime, world.context())
    assert result.status is Status.PENDING and result.facts["reason"] == "design_issue"
    assert "日期过滤散落在各处" in result.facts["gate"]["decision"]
    assert result.facts["gate"]["command"] == "tightrein approve 0018"
    # 用户同意按设计层面修复：一经记录持续有效，并写给方案
    fake = _install(monkeypatch, flagged)
    accepted = Decision("implement.design", "approve", None, "按设计修", format_iso(world.clock.now()))
    assert design.run(world.runtime, world.context(decisions=[accepted])).status is Status.PASSED
    assert fake.requests[0].variables["decisions"].startswith(DESIGN_ACCEPTED)
    # 用户亲自提的需求视为已同意
    _install(monkeypatch, flagged)
    context = world.context()
    context.issue = replace(context.issue, origin="user")
    assert design.run(world.runtime, context).status is Status.PASSED


def test_full_autonomy_accepts_a_design_level_fix(new_world: Any, settings_with: Any,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    world = new_world(settings=settings_with({"boundaries": {"gates": {"design": "auto_all"}}}))
    _install(monkeypatch, _plan(flags={**_plan()["flags"], "design": {"flagged": True, "reason": "日期过滤散落在各处"}}))
    assert design.run(world.runtime, world.context()).status is Status.PASSED


def test_a_declined_design_issue_stops(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    waiting = world.handoff(design.POINT, {"version": 1, "reason": "design_issue"}, Status.PENDING)
    world.clock.advance(timedelta(minutes=5))
    declined = Decision("implement.design", "reject", None, "找作者讨论", format_iso(world.clock.now()))
    result = design.run(world.runtime, world.context(latest={design.POINT: waiting}, decisions=[declined]))
    assert result.status is Status.FAILED and result.facts["reason"] == "design_declined"
    assert "找作者讨论" in result.summary and fake.requests == []


def test_an_oversized_issue_goes_back_to_assessment(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    parts = [{"title": f"第 {number} 部分", "goal": "单独合入", "acceptance": ["能用"]} for number in (1, 2)]
    _install(monkeypatch, _plan(oversize={"reason": "要改 30 个文件", "parts": parts}))
    result = design.run(world.runtime, world.context())
    assert result.status is Status.FAILED and result.facts["reason"] == "oversize"
    assert result.facts["splitBack"]["parts"] == parts


def _others(world: Any) -> None:
    """其他 Issue：0019 待修(镜像为 GitHub #25)，0020 已完成，0021 在等本 Issue(0018)，0022 已交付等合并。"""
    for issue_id, status, extra in (("0019", "todo", {"github": {"number": 25}}), ("0020", "done", {}),
                                    ("0021", "todo", {"blockedBy": ["0018"]}), ("0022", "releasing", {})):
        issues.save(world.runtime.conn, Issue(id=issue_id, status=status, title=f"功能 {issue_id}", kind="feature",
                                              origin="user", extra=extra), world.clock)


def test_waiting_for_other_issues_ends_the_step_without_asking(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """要等别的 Issue 先合入：不停在关卡、不问用户，交接带上前置 Issue；编号不对的带原因重出。"""
    _others(world)
    wrong = _plan(blockedBy={"issues": ["0018", "0020", "0021", "0099"], "reason": "要用排序接口"})
    fake = _install(monkeypatch, wrong, _plan(blockedBy={"issues": ["0019", "0022"], "reason": "要用 #25 的排序接口"}))
    result = design.run(world.runtime, world.context())
    assert result.status is Status.FAILED and result.facts["reason"] == design.BLOCKED
    assert result.facts["blockedBy"] == ["0019", "0022"] and "gate" not in result.facts
    assert result.summary == "要等 0019、0022 先合入：要用 #25 的排序接口"
    assert fake.requests[1].variables["feedback"].splitlines() == [
        "- blockedBy 写了本 Issue 自己(0018)",
        "- blockedBy 中的 0020 已是 done，不用等：按主干上的现状出方案",
        "- blockedBy 中的 0021 也在等本 Issue 合入，互相等待：本 Issue 不能等它",
        "- blockedBy 中的 0099 不存在：只能写「其他未完成的 Issue」中的本地编号"]
    # 方案看得到其他还没合入的 Issue：本地编号、GitHub 编号、状态与标题；已完成的与本 Issue 不列
    listed = fake.requests[0].variables["open_issues"]
    assert "- 0019(GitHub #25)，todo：功能 0019" in listed and "- 0022，releasing：功能 0022" in listed
    assert "0020" not in listed and "0018" not in listed and listed.startswith("<external")


def test_design_documents_are_readable_and_listed(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """工作区有 designs/ 时作为额外可读目录交给方案与前端设计说明，提示里列出其中的文件；没有时写「无」。"""
    fake = _install(monkeypatch, _plan())
    design.run(world.runtime, world.context())
    assert fake.requests[0].read_paths == () and fake.requests[0].variables["designs"] == "无"
    designs = world.runtime.workspace.designs_dir
    (designs / "ui").mkdir(parents=True)
    (designs / "2026-10-06-question-relevance.md").write_text("# 设计\n", encoding="utf-8")
    (designs / "ui" / "orders.md").write_text("# 订单\n", encoding="utf-8")
    (designs / ".DS_Store").write_text("", encoding="utf-8")
    plan = _plan(files=[*_plan()["files"], {"path": "web/Orders.vue", "isNew": True, "reason": "新页面"}])
    sketch = {"pages": [{"location": "/orders", "structure": "表格"}], "layout": [], "interactions": [], "states": [],
              "styling": [], "mobile": [], "copy": [], "planConflicts": []}
    fake = _install(monkeypatch, plan, frontend_outputs=[sketch])
    design.run(world.runtime, world.context())
    assert [request.read_paths for request in fake.requests] == [(designs,), (designs,)]
    assert fake.requests[1].variables["designs"] == fake.requests[0].variables["designs"] == (
        f"目录 `{designs}`，其中：\n- `designs/2026-10-06-question-relevance.md`\n- `designs/ui/orders.md`")


def test_replanning_stops_at_the_limit(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, None, _plan(estimate={"files": 99, "lines": 4}))  # 没有结果也算一次
    result = design.run(world.runtime, world.context())
    assert result.status is Status.FAILED and result.facts["reason"] == "plan_rejected"
    assert len(fake.requests) == 2 and len(result.facts["problems"]) == 2


def test_replanning_reuses_the_locating_and_carries_the_reasons(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    previous = world.handoff(design.POINT, {**_plan(), "version": 1, "planHash": "a" * 64})
    world.clock.advance(timedelta(minutes=5))
    rejected = world.handoff("implement.approve", {"rejected": True, "note": "不要改接口"}, Status.FAILED,
                             at=world.clock.now())
    gap = {"check": "code", "kind": "big_issue", "location": "src/orders.py:2", "summary": "方案走不通",
           "category": "plan_gap", "trigger": None}
    coded = world.handoff("implement.code", {"blockers": [gap]}, Status.FAILED, at=world.clock.now())
    fake = _install(monkeypatch, _plan())
    context = world.context(latest={design.POINT: previous, "implement.approve": rejected, "implement.code": coded})
    result = design.run(world.runtime, context)
    assert result.facts["version"] == 2  # 上一版通过后重出才加一
    assert fake.requests[0].variables["replan"] == ("- 用户否决了上一版方案：不要改接口\n"
                                                    "- [code/big_issue] src/orders.py:2：方案走不通")


def test_plans_with_frontend_files_get_a_frontend_design(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(files=[*_plan()["files"], {"path": "web/Orders.vue", "isNew": True, "reason": "新页面"}])
    sketch = {"pages": [{"location": "/orders", "structure": "表格"}], "layout": [], "interactions": [], "states": [],
              "styling": [{"target": "表头", "value": "var(--gray-1)", "source": "web/theme.css:3"}], "mobile": [],
              "copy": [], "planConflicts": ["分页也要改"]}
    fake = _install(monkeypatch, copy.deepcopy(plan), frontend_outputs=[sketch])
    result = design.run(world.runtime, world.context())
    assert [request.point for request in fake.requests] == [design.POINT, frontend.POINT]
    assert result.facts["frontendFiles"] == ["web/Orders.vue"] and result.facts["frontendDesign"] == sketch
    # 失败不阻断方案，原因记下
    _install(monkeypatch, copy.deepcopy(plan), frontend_outputs=[None])
    failed = design.run(world.runtime, world.context())
    assert failed.status is Status.PASSED and failed.facts["frontendDesign"] is None
    assert failed.facts["frontendError"].startswith("前端设计说明没有产出")
