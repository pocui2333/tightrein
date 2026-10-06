import json

import pytest
from pipeline_world import NOW, make_signal
from store_problem import make_problem
from triage_world import triage_outputs

from tightrein.domain import issue_sections
from tightrein.domain.enums import Probe
from tightrein.domain.problem import ProblemScope
from tightrein.evaluation.rubric import RUBRICS_DIR
from tightrein.pipeline.issue.render import issue as template
from tightrein.pipeline.issue.steps import acceptance, body, slug
from tightrein.store.files import markdown

SERVER = make_signal(probe=Probe.PLATFORM_ERRORS, check="exception", location="OrderService.Get",
                     context={"exceptionType": "System.NullReferenceException",
                              "projectFrames": [{"symbol": "OrderService.Get", "file": "src/A.src", "line": 3}]})
REPRODUCE = "本 Issue 的复现检查在修复后通过"


@pytest.mark.parametrize("problem, signal, expected", [
    (make_problem(scope=ProblemScope("POST /api/Material/Query")), make_signal(), "material-query-500"),
    (make_problem(), make_signal(check="unauthorized_role_access", context={}), "api-order-unauthorized-role-access"),
    (make_problem(probe=Probe.PLATFORM_ERRORS, scope=ProblemScope("OrderService.Get")), SERVER,
     "nullreferenceexception-get"),
    (make_problem(probe=Probe.STATIC, scope=ProblemScope("src/A.src:OrderService.Get")),
     make_signal(probe=Probe.STATIC, check="DP-0001"), "dp-0001-get"),
    (make_problem("P-0012", probe=Probe.ALERTS, scope=ProblemScope("订单停滞")),
     make_signal(probe=Probe.ALERTS, check="business-alert", location="订单停滞"), "business-alert"),
])
def test_slugs_per_probe(problem, signal, expected):
    assert slug.slug(problem, signal, 40) == expected


def test_slugs_are_truncated_without_a_trailing_dash():
    problem = make_problem(scope=ProblemScope("GET /api/VeryLongControllerName/AnotherVeryLongActionName"))
    assert slug.slug(problem, make_signal(), 30) == "verylongcontrollername-another"


def test_acceptance_per_probe():
    assert acceptance.build(make_problem(), make_signal()) == [
        "api-fuzz 对 `GET /api/Order/{id}` 的 `not_a_server_error` 检查以 `Admin` 身份通过", REPRODUCE]
    server = acceptance.build(make_problem(probe=Probe.PLATFORM_ERRORS), SERVER)
    assert server == ["部署后的观察期与之后的覆盖运行中不再出现指纹为 `a1b2c3d4e5f60718` 的问题", REPRODUCE]
    static = acceptance.build(make_problem(probe=Probe.STATIC, scope=ProblemScope("src/A.src:OrderService.Get")),
                              make_signal(probe=Probe.STATIC, check="DP-0001"))
    assert static[0] == "静态巡检的 `DP-0001` 规则在 `src/A.src:OrderService.Get` 不再命中"
    assert acceptance.build(make_problem(probe=Probe.INCIDENTAL), None)[:2] == [
        "对原发现重新取证，判定为不成立", "修复环节建立的复现检查通过"]


def render(outputs, criteria, language="zh", history="- h"):
    found = body.sections(outputs, criteria, language)
    references = body.references(outputs, "data/findings/P-0001.md", make_problem(), language)
    text = template.document(body.conclusion(outputs, body.title(outputs, 40), language), found, references,
                             [template.approve_step("0001", language)], history, language)
    return found, text


def test_body_sections_follow_the_handoff_layout_and_carry_the_triage_findings():
    rubric = json.loads((RUBRICS_DIR / "issue.json").read_text(encoding="utf-8"))
    outputs = triage_outputs(labels=["discuss-with-author"],
                             flags={"design": {"flagged": True, "reason": "状态放在两处", "locations": ["src/A.src:3"]},
                                    "dataStructure": {"flagged": False}, "publicContract": {"flagged": False}},
                             scope={"outOfScope": ["不改导出格式"], "mustKeep": ["公共接口 OrderService.Get 的签名"]})
    history = template.history_line(NOW, "创建，运行 R-20261005-030000-issue")
    found, text = render(outputs, ["条件一"], history=history)
    assert set(rubric["items"][0]["params"]["sections"]) <= issue_sections.keys_in(text)
    assert [line[3:] for line in text.splitlines() if line.startswith("## ")] == [
        "结论", "内容", "需要决定", "下一步", "引用", "历史"]
    assert [line[4:] for line in text.splitlines() if line.startswith("### ")] == [
        "问题", "影响", "复现", "原因", "范围", "注意事项", "验收标准", "修复方向"]
    assert found["reproduce"] == ("1. Admin 以外的角色传入其他公司的订单编号\n\n"
                                  "- 复现命令：`curl -X GET /api/Order/42`")
    assert found["scope"] == "- 可能涉及的文件：`src/Services/OrderService.src`\n- 不在本次范围内：不改导出格式"
    assert found["notes"].startswith("- **需要先与代码作者讨论**\n- 不能改：公共接口 OrderService.Get 的签名")
    assert "需用户定夺：根因在设计本身：状态放在两处(`src/A.src:3`)" in found["notes"]
    assert found["direction"].startswith("在 OrderService.Get 中按公司过滤")
    assert found["acceptance"] == ("- [ ] 复现测试在修复前失败、修复后通过\n- [ ] 现有测试全部通过\n"
                                   "- [ ] 保持不变：公共接口 OrderService.Get 的签名\n- [ ] 条件一")
    assert "- `src/Services/OrderService.src:12` id 直接用于查询，没有按公司过滤" in text
    assert "- [ ] 审阅并放行：tightrein approve 1(user)" in text
    assert issue_sections.find(issue_sections.split(text), issue_sections.HISTORY).startswith("- 2026-10-05 ")


def test_the_report_fills_the_sections_in_the_project_language():
    report = {"title": "[Orders] Order lookup returns 500 for another company's order", "summary": "Lookup fails.",
              "steps": ["Log in as Company", "GET /api/Order/42"], "expected": "403", "actual": "500",
              "acceptance": ["GET /api/Order/42 returns 403"], "severity": "P1", "severityReason": "core flow"}
    outputs = triage_outputs(report=report, severity="P1", scope={"outOfScope": [], "mustKeep": []})
    found, text = render(outputs, ["probe check"], "en")
    assert text.startswith("## Conclusion\n\n[Orders] Order lookup returns 500 for a…")
    assert "### Problem\n\nLookup fails.\n\n- Expected：403\n- Actual：500" in text
    assert found["reproduce"].startswith("1. Log in as Company\n2. GET /api/Order/42")
    assert found["acceptance"].endswith("- [ ] GET /api/Order/42 returns 403\n- [ ] probe check")
    assert "Apart from the problem described here" in found["acceptance"]
    assert found["impact"].startswith("- Severity：P1(core flow)")
    assert body.missing_fields(outputs) == [] and "title" in body.missing_fields(triage_outputs())


def test_docs_config_issues_say_no_repro_test_is_written():
    found = body.sections(triage_outputs(), [], "zh", repro_test=False)
    assert found["acceptance"].startswith("- [ ] 复现测试：本类型不写(fix.repro.skipTypes)")


def test_history_and_references_are_appended_to_their_sections():
    text = template.document("x", {name: "- 第一行" for name in issue_sections.CONTENT_KEYS}, [], [], "- 创建", "zh")
    appended = template.append_related(template.append_history(text, NOW, "关闭", None), "P-0002", "订单页请求失败")
    parsed = markdown.parse("---\nid: 1\n---\n" + appended)
    assert "## 引用\n\n- `P-0002` 订单页请求失败\n\n## 历史" in parsed.body
    assert parsed.body.rstrip().endswith("关闭")
    legacy = "# x\n\n## 结论\n\n旧\n\n## 关联\n\n- P-0001\n\n## 历史\n\n- 一\n"
    assert template.append_history(legacy, NOW, "二", None).rstrip().endswith("二")
    assert "- P-0001\n- P-0002 t" in template.append_related(legacy, "P-0002", "t")
    assert issue_sections.find(issue_sections.split(legacy), issue_sections.PROBLEM) == "旧"


def test_manual_requirements_keep_their_acceptance_criteria():
    requirement = "导出加一列。\n\n## 验收标准\n\n- [ ] 导出文件有「来源」列\n"
    content = template.manual_content(requirement, "zh")
    assert content["problem"] == "导出加一列。"
    assert content["acceptance"].endswith("- [ ] 导出文件有「来源」列")
    assert content["cause"] == "—"
