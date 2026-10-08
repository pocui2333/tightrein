"""实施各调用点共用的拼装：验收标准、方案字段裁剪、用户的决定、外部内容包住；各模板的变量与程序给的一致。"""

from __future__ import annotations

from typing import Any

from tightrein.agents.params import FRONTEND
from tightrein.assess.notes import CORE, CodeNotes, NoteEntry
from tightrein.implement.code.code import SCHEMA as CODE_SCHEMA
from tightrein.implement.context import Decision, Risk
from tightrein.implement.design.design import SCHEMA as DESIGN_SCHEMA
from tightrein.implement.design.frontend import SCHEMA as FRONTEND_SCHEMA
from tightrein.implement.locate.locate import SCHEMA as LOCATE_SCHEMA
from tightrein.implement.prompts import code, common, design, frontend, locate
from tightrein.prompts.build import build
from tightrein.protocol.handoff import load_schema

PLAN = {"summary": "改为最近 7 天", "analysis": "推理过程", "estimate": {"files": 1, "lines": 4},
        "hypothesis": {"cause": "查询条件写成当天", "evidence": [{"location": "src/orders.py:2", "fact": "日志"}],
                       "edits": [{"location": "src/orders.py:2", "change": "按 days 过滤"}]},
        "steps": [{"file": "src/orders.py", "change": "改查询", "verification": "pytest"}],
        "files": [{"path": "src/orders.py", "isNew": False, "reason": None}], "protectedTouches": [],
        "migration": None, "notDoing": ["不改分页"], "acceptance": ["列表显示最近 7 天的订单"], "frontendDesign": None}


def test_acceptance_criteria_are_read_from_their_section() -> None:
    body = ("## 问题\n\n- 不是验收标准\n\n## 验收标准\n\n- 列表显示 7 天\n1. 接口返回 200\n- [x] 已勾选的也算\n"
            "### 细节\n\n- 子标题下的也算\n\n## 备注\n\n- 不算\n")
    assert common.acceptance(body) == ["列表显示 7 天", "接口返回 200", "已勾选的也算", "子标题下的也算"]
    assert common.acceptance("## Acceptance criteria\n\n* works\n") == ["works"]
    assert common.acceptance("没有这一节") == []


def test_criteria_confirmed_only_after_deploy_are_left_to_acceptance() -> None:
    """方案不要求对应、审查不判断部署后才能确认的标准(44 号计划 6「验收：只做实施阶段做不到的」)。"""
    body = ("## 验收标准\n\n- [ ] average([]) 返回 0\n"
            "- [ ] 部署后的观察期与之后的覆盖运行中不再出现指纹为 `average:fp` 的问题\n"
            "- [ ] No problem with fingerprint `x` appears in the post-deploy observation window and later covering runs\n")
    assert common.acceptance(body) == ["average([]) 返回 0"]


def test_the_plan_view_drops_the_evidence_and_empty_fields() -> None:
    view = common.plan_view(PLAN, common.PLAN_FOR_CODE)
    assert view["hypothesis"] == {"cause": "查询条件写成当天",
                                  "edits": [{"location": "src/orders.py:2", "change": "按 days 过滤"}]}
    assert "analysis" not in view and "estimate" not in view
    assert "protectedTouches" not in view and "migration" not in view  # 空的不占篇幅


def test_decisions_and_external_text(world: Any) -> None:
    assert common.decisions_text([]) == common.NONE
    decisions = [Decision("implement.approve", "approve", 2, "按选项 2", "2026-10-08T03:00:00Z"),
                 Decision("implement.design", "reject", None, None, "2026-10-08T04:00:00Z")]
    assert common.decisions_text(decisions) == ("- 2026-10-08T03:00:00Z(implement.approve，approve，选项 2)：按选项 2\n"
                                                "- 2026-10-08T04:00:00Z(implement.design，reject)：无补充")
    text = common.issue_text(world.context())
    assert text.startswith("<external") and "订单列表只显示当天" in text


def test_the_issue_text_is_the_body_without_a_second_title(world: Any) -> None:
    body = "# 0007 订单列表只显示当天\n\n## 问题\n\n订单列表只显示当天的订单。\n"
    text = common.issue_text(world.context(body=body))  # Issue 标题同为「订单列表只显示当天」
    assert text.count("订单列表只显示当天\n") == 1 and "# 0007 订单列表只显示当天\n" in text


def test_every_template_gets_exactly_its_variables(world: Any) -> None:
    """模板与程序给的变量对不上时 build 直接报错：各调用点都拼一次。"""
    accepted = Decision("implement.design", "approve", None, "按设计修", "2026-10-08T03:00:00Z")
    context = world.context(decisions=[accepted])
    entry = NoteEntry("src/orders.py:2", CORE, "只取当天", "2  return days")
    context.notes = CodeNotes("0018", world.repo.base, [entry], ["src/orders.py"])
    runtime = world.runtime
    built = {
        "implement.locate": (locate.variables(runtime, context, ["位置越界"]), LOCATE_SCHEMA),
        "implement.design": (design.variables(runtime, context, risk=Risk(True, ("schema：含迁移",)), replan=[],
                                              feedback=[], design_accepted=True), DESIGN_SCHEMA),
        "implement.design.frontend": (frontend.variables(runtime, context, PLAN, ["web/Orders.vue"]), FRONTEND_SCHEMA),
        "implement.code": (code.variables(runtime, context, PLAN, ["修正一处"]), CODE_SCHEMA),
        code.CONTINUE: (code.continue_variables(context, ["修正一处"]), CODE_SCHEMA),
    }
    texts = {point: build(point, variables, language="zh", schema=load_schema(schema), tool="claude").text
             for point, (variables, schema) in built.items()}
    assert all(text.startswith("# 角色") for text in texts.values())
    # 定位的工作方式：先用笔记、先搜索后阅读、有着落即停；错误的已有实现比「无」危害大
    assert "停止条件" in texts["implement.locate"] and "先搜索、后阅读" in texts["implement.locate"]
    assert "错误的已有实现比「无」危害大" in texts["implement.locate"]
    # 方案的要求：只在现有架构里修、改动最少、排除不到一条交用户
    assert "只在现有架构里修" in texts["implement.design"] and "改动最少" in texts["implement.design"]
    assert "不同时按几种可能各改一点" in texts["implement.design"]
    # 编码的禁令与「自述只写证据」；方案与笔记已在提示里，不再给交接文件的路径
    assert "加抑制注释" in texts["implement.code"] and "没运行的写「未运行」" in texts["implement.code"]
    assert "handoff" not in texts["implement.code"] and "notes.json" not in texts["implement.code"]
    assert "抑制注释" in texts[code.CONTINUE] and "最小修改" not in texts[code.CONTINUE]  # 续接不重复规则
    locate_text = built["implement.locate"][0]
    assert locate_text["feedback"] == "- 位置越界" and "只取当天" in locate_text["notes"]
    design_text = built["implement.design"][0]
    assert design_text["risk"].startswith("高风险") and "10 个文件、400 行" in design_text["limits"]
    assert design_text["decisions"].startswith(design.DESIGN_ACCEPTED)
    assert built[code.CONTINUE][0]["round"] == "1"


def test_locating_uses_the_frontend_condition_for_frontend_notes(world: Any) -> None:
    context = world.context()
    assert locate.conditions(world.runtime, context) == ()
    context.notes = CodeNotes("0018", world.repo.base, files=["web/Orders.vue"])
    assert locate.conditions(world.runtime, context) == (FRONTEND,)
