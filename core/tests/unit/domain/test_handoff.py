from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import DocumentStatus
from tightrein.domain.handoff import sections, types
from tightrein.domain.handoff.document import (
    Decision,
    Event,
    HandoffDocument,
    Header,
    NextStep,
    Reference,
    RenderError,
    render_body,
)

AT = datetime(2026, 10, 2, 2, 15, tzinfo=timezone.utc)


def plan(**changes):
    values = dict(
        header=Header("plan", "PL-20261002-0001", DocumentStatus.PENDING, "fix/fix-planner", "fix/fix-executor",
                      "0003", AT, AT, parent="TR-20261002-0003"),
        conclusion="在 OrderService.create 中补上数据归属校验。",
        sections={"approach": "按 owner 过滤。", "steps": "1. 改服务层。", "acceptanceMapping": "A1 对应复现测试。",
                  "outOfScope": "不改接口签名。"},
        blocks={"files": [{"path": "src/orders.py", "change": "modify"}]},
        decisions=(Decision("是否同时修复导出接口", "另开 Issue", "范围不同"),),
        next_steps=(NextStep("实施计划", "fix/fix-executor"),),
        references=(Reference("data/fixes/0003/plan.md", "上一版计划"),),
        history=(Event(AT, "创建"),),
    )
    values.update(changes)
    return HandoffDocument(**values)


def dump(data):
    return repr(data)


def test_every_section_key_has_three_language_headings():
    keys = {key for doc in types.TYPES.values() for key in doc.sections} | set(types.BASE_SECTIONS)
    assert keys <= set(types.HEADINGS)
    assert all(set(titles) == {"zh", "en", "ja"} for titles in types.HEADINGS.values())
    for doc in types.TYPES.values():
        assert set(doc.blocks.values()) <= set(doc.sections) and doc.required_blocks <= set(doc.blocks)
        distinct = sum(len(set(types.HEADINGS[key].values())) for key in (*types.BASE_SECTIONS, *doc.sections))
        assert len(types.aliases((*types.BASE_SECTIONS, *doc.sections))) == distinct - _shared_with_base(doc)


def _shared_with_base(doc):
    """review 的「结论」小节与基础的「结论」同名，按标题级别区分。"""
    base = types.aliases(types.BASE_SECTIONS)
    return sum(1 for key in doc.sections for title in set(types.HEADINGS[key].values()) if title in base)


def test_render_writes_the_base_sections_in_order_with_headings_in_the_project_language():
    body = render_body(plan(), "en", dump, timezone.utc)
    assert [title for title in sections.split(body)] == [
        "Conclusion", "Content", "Decisions needed", "Next steps", "References", "History"]
    assert list(sections.split(sections.split(body)["Content"], 3)) == [
        "Approach", "Files and steps", "Acceptance mapping", "Out of scope"]
    assert "```yaml data:files\n[{'path': 'src/orders.py', 'change': 'modify'}]\n```" in body
    assert "- 是否同时修复导出接口 —— Recommendation：另开 Issue；Reason：范围不同" in body
    assert "- [ ] 实施计划(fix/fix-executor)" in body
    assert "- 2026-10-02 02:15(UTC) 创建" in body


def test_empty_lists_are_written_as_none():
    body = render_body(plan(decisions=(), next_steps=(), references=(), history=()), "zh", dump)
    split = sections.split(body)
    assert [split[title].strip() for title in ("需要决定", "下一步", "引用", "历史")] == ["无"] * 4


def test_render_refuses_missing_sections_and_unknown_blocks():
    with pytest.raises(RenderError, match="缺少小节 outOfScope.*没有数据块 extra"):
        render_body(plan(sections={**plan().sections, "outOfScope": " "},
                         blocks={**plan().blocks, "extra": []}), "zh", dump)
    with pytest.raises(RenderError, match="缺少数据块 files"):
        render_body(plan(blocks={}), "zh", dump)
    with pytest.raises(types.UnknownKind):
        render_body(plan(header=Header("memo", "M-1", DocumentStatus.DONE, "a", "b", "c", AT, AT)), "zh", dump)


def test_extract_takes_the_requested_sections_in_any_language():
    text = "---\nkind: plan\n---\n" + render_body(plan(), "ja", dump)
    excerpt = sections.extract(text, ["conclusion", "outOfScope", "decisions", "goal"], "plan")
    assert excerpt.startswith("## 結論\n\n在 OrderService.create 中补上数据归属校验。\n\n### 対象外\n\n不改接口签名。")
    assert "## 要判断事項" in excerpt and "## 次のステップ" not in excerpt
    assert sections.extract(text, ["goal"]) == ""


def test_append_line_and_language_detection():
    body = "## 问题\n\n旧\n\n## 历史\n\n- 第一行\n"
    appended = sections.append_line(body, "历史", "- 第二行")
    assert appended.endswith("- 第一行\n- 第二行\n")
    assert sections.append_line("## 问题\n", "关联", "- x").endswith("## 关联\n\n- x\n")
    assert sections.language_of("## Problem\n", {"problem": types.HEADINGS["problem"]}, "zh") == "en"
    assert sections.language_of("## 其他\n", {"problem": types.HEADINGS["problem"]}, "zh") == "zh"


def test_a_fence_closes_only_with_the_same_kind_and_at_least_the_same_length():
    tilde_inside = "## A\n```\n~~~\n## Fake\n```\n## B\nx\n"
    assert list(sections.split(tilde_inside)) == ["A", "B"]
    longer_outside = "## A\n````\n```\n## Fake\n```\n````\n## B\nx\n"
    assert list(sections.split(longer_outside)) == ["A", "B"]
