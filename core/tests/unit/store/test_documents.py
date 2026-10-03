from datetime import datetime, timedelta, timezone

import pytest

from tightrein.contracts import validate
from tightrein.domain.enums import DocumentStatus
from tightrein.domain.handoff import types
from tightrein.domain.handoff.document import Event, HandoffDocument, Header, NextStep, Reference
from tightrein.store.files import documents

AT = datetime(2026, 10, 2, 2, 15, tzinfo=timezone.utc)
LIMITS = {"conclusion": 400, "default": 4000}


def review(**changes):
    values = dict(
        header=Header("review", "RV-20261002-0001", DocumentStatus.DONE, "fix/fix-reviewer", "fix", "0003", AT, AT,
                      parent="RS-20261002-0002", next="fix apply 0003"),
        conclusion="有一个阻断项：缺少 owner 校验。",
        sections={"verdict": "不通过。", "findings": "见下方清单。", "basis": "计划第 2 步与 diff。"},
        blocks={"issues": [{"level": "blocking", "text": "缺少对 owner 的校验", "location": "src/orders.py:42"}]},
        next_steps=(NextStep("按阻断项修改", "fix/fix-executor"),),
        references=(Reference("data/fixes/0003/diff.patch", "本轮 diff"),),
    )
    values.update(changes)
    return HandoffDocument(**values)


def test_each_registered_type_has_a_block_schema_with_one_definition_per_label():
    names = {name for name in validate.names() if name.startswith("handoff/types/")}
    assert names == {doc.schema for doc in types.TYPES.values()}
    for doc in types.TYPES.values():
        assert set(validate.schema(doc.schema)["$defs"]) == set(doc.blocks)


def test_written_documents_read_back_with_header_sections_and_blocks(tmp_path):
    path = tmp_path / "review.md"
    text = documents.write(path, review(), "zh", timezone.utc)
    assert text.startswith("---\nkind: review\nid: RV-20261002-0001\nstatus: done\nfrom: fix/fix-reviewer\n")
    parsed = documents.read(path)
    assert parsed.status is DocumentStatus.DONE and parsed.header["parent"] == "RS-20261002-0002"
    assert parsed.conclusion.strip() == "有一个阻断项：缺少 owner 校验。"
    assert parsed.sections == {"verdict": "不通过。", "findings": "见下方清单。", "basis": "计划第 2 步与 diff。"}
    assert parsed.blocks["issues"][0]["location"] == "src/orders.py:42"
    assert documents.check(text, LIMITS) == []


def test_write_refuses_blocks_that_break_the_schema(tmp_path):
    bad = review(blocks={"issues": [{"level": "minor", "text": "x"}]})
    with pytest.raises(documents.DocumentError, match="数据块 issues"):
        documents.write(tmp_path / "review.md", bad, "zh")
    assert not (tmp_path / "review.md").exists()


def test_check_lists_every_problem():
    text = documents.render(review(), "en")
    broken = (text.replace("### Basis", "### Grounds").replace("```yaml data:issues", "```yaml data:notes")
              .replace("## References", "## Links"))
    assert documents.check(broken) == ["缺少小节「引用」", "缺少「内容」小节「依据」", "类型 review 没有数据块 notes",
                                       "缺少数据块 issues"]
    assert documents.check("no header") == ["<text> 没有以 --- 开头的 frontmatter"]
    unknown = text.replace("kind: review", "kind: memo")
    assert documents.check(unknown) == ["<text>：没有登记的文档类型：memo"]
    header = text.replace("status: done", "status: finished")
    assert "头信息不符合 handoff/document.schema.json" in documents.check(header)[0]


def test_check_enforces_the_order_of_base_sections_and_the_length_limits():
    text = documents.render(review(conclusion="长" * 401), "zh")
    assert documents.check(text, LIMITS) == [
        "「结论」有 401 个字符，超过上限 400；超出的部分放进单独的附件文件，正文只给引用与摘要"]
    assert documents.check(text, {"default": 4000}) == []
    swapped = text.replace("## 下一步", "## 临时").replace("## 需要决定", "## 下一步").replace("## 临时", "## 需要决定")
    assert documents.check(swapped) == ["基础小节的顺序应为：结论、内容、需要决定、下一步、引用、历史"]


def test_history_is_only_appended_and_status_lives_in_the_header(tmp_path):
    path = tmp_path / "review.md"
    documents.write(path, review(), "zh", timezone.utc)
    later = AT + timedelta(minutes=6)
    documents.append_history(path, later, "交给 fix-executor 修改", DocumentStatus.IN_PROGRESS, timezone.utc)
    documents.append_history(path, later + timedelta(minutes=1), "修改完成", zone=timezone.utc)
    parsed = documents.read(path)
    assert parsed.status is DocumentStatus.IN_PROGRESS and parsed.header["updated"] == "2026-10-02T02:22:00Z"
    assert parsed.body.endswith("## 历史\n\n- 2026-10-02 02:21(UTC) 交给 fix-executor 修改\n"
                                "- 2026-10-02 02:22(UTC) 修改完成\n")
    assert documents.check(path.read_text(encoding="utf-8"), LIMITS) == []


def test_history_keeps_existing_events(tmp_path):
    path = tmp_path / "review.md"
    documents.write(path, review(history=(Event(AT, "创建"),)), "en", timezone.utc)
    documents.append_history(path, AT + timedelta(hours=1), "Reviewed", zone=timezone.utc)
    assert documents.read(path).body.endswith("## History\n\n- 2026-10-02 02:15(UTC) 创建\n"
                                              "- 2026-10-02 03:15(UTC) Reviewed\n")


def test_headings_in_model_text_do_not_break_the_document_structure():
    document = review()
    text = "### 查勘与分析过程\n\n1. 读了代码。\n\n## 根因\n\n```md\n## 代码块中的行不动\n```"
    rendered = documents.render(HandoffDocument(document.header, "# 结论里的标题", {**document.sections, "verdict": text},
                                                document.blocks, references=document.references,
                                                history=document.history), "zh")
    assert documents.check(rendered) == []
    assert "###### 查勘与分析过程" in rendered and "##### 根因" in rendered and "\n## 代码块中的行不动\n" in rendered
