import pytest

from tightrein.assess.issue import body
from tightrein.assess.issue.body import IssueFacts

CLAIM = {"statement": "Admin 调用 GET /orders/{id} 时服务端返回 500", "title": "订单详情报错", "facts": [],
         "entryPoints": []}


def facts(output: dict, **changes) -> IssueFacts:
    values = {"issue": "0007", "title": "[订单] 可以读到他人的订单", "severity": "P1", "output": output, "claim": CLAIM,
                  "commit": "c" * 40, "introduced": ({"commit": "a" * 40, "author": "zhang", "pr": 12},),
                  "labels": (), "probe_acceptance": ("部署后不再出现指纹为 `fp` 的问题",),
                  "problems": (("P-0001", "保存订单时报错"),)}
    values.update(changes)
    return IssueFacts(**values)


def test_body_sections_follow_the_layout_and_carry_the_assessment(good_output):
    output = good_output()
    output["assessment"]["flags"]["dataStructure"] = {"flagged": True, "reason": "要给订单加用户列",
                                                      "locations": ["services/orders.py:3"]}
    text = body.render(facts(output, labels=(body.DISCUSS,)), "zh")
    sections = body.split(text)
    assert list(sections) == ["conclusion", "problem", "impact", "reproduce", "cause", "scope", "notes", "acceptance",
                              "direction", "references"]
    assert "不在本次范围内：其他列表接口" in sections["scope"]  # 明确不做写进范围
    assert "保持不变：接口返回结构不变" in sections["notes"]  # 必须保持不变写进注意事项
    assert "需用户定夺：要动数据结构或存量数据：要给订单加用户列(`services/orders.py:3`)" in sections["notes"]
    assert "**需要先与代码作者讨论" in sections["notes"]
    assert sections["acceptance"] == "- [ ] 用户 A 请求用户 B 的订单返回 404\n- [ ] 部署后不再出现指纹为 `fp` 的问题"
    assert "根因位置：`services/orders.py:3` OrderService.get" in sections["cause"]  # 代码位置写在反引号里
    assert "引入：aaaaaaaaaaaa(PR #12)" in sections["cause"]
    assert "严重度：P1(只影响少量订单，可以绕开)" in sections["impact"]
    assert "—" not in text


def test_empty_sections_are_left_out_and_no_generic_acceptance_is_added(good_output):
    output = good_output(report=None, assessment=None, impact=None, trigger=None, counterEvidence=[])
    text = body.render(facts(output, probe_acceptance=(), severity=None), "zh")
    sections = body.split(text)
    assert "impact" not in sections and "scope" in sections and "direction" not in sections
    assert "acceptance" not in sections  # 不再加三条固定的通用验收项
    assert sections["problem"] == CLAIM["statement"]


def test_titles_are_truncated_with_an_ellipsis(good_output):
    output = good_output()
    assert body.title_of(output, CLAIM, 80) == "[订单] 可以读到他人的订单"
    assert body.title_of(output, CLAIM, 6) == "[订单]…"
    assert body.title_of({"report": None}, CLAIM, 80) == "订单详情报错"


@pytest.mark.parametrize("source, role, expected", [
    ("collect.api_fuzz", "Viewer", "API 模糊测试对 `GET /a` 的 `server_error` 检查以 `Viewer` 身份通过"),
    ("collect.api_fuzz", None, "API 模糊测试对 `GET /a` 的 `server_error` 检查以 `未登录` 身份通过"),
    ("collect.platform_errors", None, "部署后的观察期与之后的覆盖运行中不再出现指纹为 `fp` 的问题"),
    ("collect.static", None, "静态巡检的 `server_error` 在 `GET /a` 不再命中"),
    ("collect.incidental", None, "对原发现重新取证，判定为不成立"),
])
def test_acceptance_per_probe(source, role, expected):
    assert body.probe_acceptance(source, "GET /a", "server_error", "fp", role, "zh") == [expected]


@pytest.mark.parametrize("language", ["zh", "en", "ja"])
def test_only_the_observed_criterion_is_confirmed_after_deploy(language):
    """观察类来源的「部署后的观察期内不再出现…」是验收阶段的标准(任一语言的原文、带不带复选框都认)，其余不是。"""
    (observed,) = body.probe_acceptance("collect.project_probes", None, "probe", "average:fp", None, language)
    assert body.post_deploy(observed) and body.post_deploy(f"[ ] {observed}")
    assert body.post_deploy_fingerprint(observed) == "average:fp"
    for source in ("collect.api_fuzz", "collect.static", "collect.incidental"):
        (other,) = body.probe_acceptance(source, "GET /a", "server_error", "fp", None, language)
        assert not body.post_deploy(other) and body.post_deploy_fingerprint(other) is None
    text = body.document("0007 t", {"acceptance": body.checkboxes(["average([]) 返回 0", observed])}, language)
    assert body.post_deploy_items(text) == [observed]


def test_sections_are_found_by_key_in_any_language_and_fences_close_properly():
    text = ("# 0007 t\n\n## Problem\n\nx\n\n````\n## 问题\n```\nstill code\n````\n\n## 受け入れ基準\n\n- [ ] ok\n\n"
            "## 自定义\n\ny\n")
    sections = body.split(text)
    assert set(sections) == {"problem", "acceptance", "自定义"}
    assert "still code" in sections["problem"] and "## 问题" in sections["problem"]  # 代码块里的 # 不切断小节
    assert body.acceptance_items(text) == ["ok"]


def test_fixed_texts_fall_back_to_the_primary_language_then_english():
    assert body.heading("history", "zh-TW") == "历史"
    assert body.heading("history", "ja_JP") == "履歴"
    assert body.heading("history", "fr") == "History"
    assert set(body.HEADINGS) == set(body.SECTIONS)
    assert all(set(titles) == {"zh", "en", "ja"} for titles in body.HEADINGS.values())


def test_history_is_appended_and_the_section_comes_back_when_removed():
    entry = {"at": "2026-10-08T03:00:00Z", "event": "approve", "actor": "user", "reason": None, "note": "看过了"}
    text = body.append_history("# 0007 t\n\n## 问题\n\nx\n", [entry], "zh")
    assert text.endswith("## 历史\n- 2026-10-08T03:00:00Z approve(操作者 user)：看过了\n")
    again = body.append_history(text, [dict(entry, event="start", note=None)], "zh")
    assert again.count("## 历史") == 1 and again.endswith("start(操作者 user)\n")


def test_user_headings_become_bold_and_acceptance_is_lifted_out():
    problem, criteria = body.requirement_parts("# 需求\n导出带备注\n### 细节\n- 列名叫备注\n## Acceptance criteria\n"
                                               "- [x] 有备注列\n* 空值留空\n")
    assert problem == "**需求**\n导出带备注\n**细节**\n- 列名叫备注"
    assert criteria == ["有备注列", "空值留空"]
