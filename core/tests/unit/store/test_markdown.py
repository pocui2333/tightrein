import pytest
import yaml

from tightrein.store.files import atomic, markdown, yaml_text
from tightrein.store.files.markdown import FrontmatterError, MarkdownDocument

TEXT = """---
type: issue
id: '0007'
updated: 2026-10-05
createdAt: 2026-10-05T03:00:00Z
score: 12.5
tags:
- 权限
- api
hold: null
---

# 订单查询缺少归属校验

正文第一段。
"""


def test_dates_stay_strings_and_other_scalars_are_typed():
    document = markdown.parse(TEXT)
    assert document.frontmatter == {
        "type": "issue", "id": "0007", "updated": "2026-10-05", "createdAt": "2026-10-05T03:00:00Z",
        "score": 12.5, "tags": ["权限", "api"], "hold": None,
    }
    assert document.body == "\n# 订单查询缺少归属校验\n\n正文第一段。\n"
    assert yaml_text.load("a: 1\nb: true\nc: 2026-10-05") == {"a": 1, "b": True, "c": "2026-10-05"}


def test_round_trip_keeps_frontmatter_and_body():
    document = markdown.parse(TEXT)
    again = markdown.parse(markdown.render(document))
    assert again == document


def test_rendered_dates_are_quoted_for_other_readers():
    text = markdown.render(MarkdownDocument({"updated": "2026-10-05", "id": "0007", "flag": "yes"}, "body"))
    assert text == "---\nupdated: '2026-10-05'\nid: '0007'\nflag: 'yes'\n---\nbody"
    assert yaml.safe_load(text.split("---")[1]) == {"updated": "2026-10-05", "id": "0007", "flag": "yes"}


def test_empty_frontmatter_and_body():
    assert markdown.parse("---\n---\n") == MarkdownDocument({}, "")
    assert markdown.render(MarkdownDocument({}, "x")) == "---\n---\nx"


@pytest.mark.parametrize("text, message", [
    ("# 标题\n", "没有以 --- 开头"),
    ("---\nid: 1\n", "没有结束行"),
    ("---\n- a\n---\n", "不是映射"),
    ("---\nid: [1\n---\n", "无法解析"),
])
def test_invalid_frontmatter(text, message):
    with pytest.raises(FrontmatterError, match=message):
        markdown.parse(text, "issues/0007-x.md")


def test_write_and_read_a_file(tmp_path):
    path = tmp_path / "knowledge" / "defect-pattern" / "DP-0012-x.md"
    document = MarkdownDocument({"id": "DP-0012", "updated": "2026-10-05"}, "正文\n")
    text = markdown.write(path, document)
    assert path.read_text(encoding="utf-8") == text
    assert markdown.read(path) == document
    assert sorted(item.name for item in path.parent.iterdir()) == ["DP-0012-x.md"]


def test_a_failed_atomic_write_keeps_the_original(tmp_path, monkeypatch):
    path = tmp_path / "a.md"
    atomic.write_text(path, "原文")

    def broken(source, target):
        raise OSError("磁盘已满")

    monkeypatch.setattr("tightrein.store.files.atomic.os.replace", broken)
    with pytest.raises(OSError):
        atomic.write_text(path, "新内容")
    assert path.read_text(encoding="utf-8") == "原文"
    assert [item.name for item in tmp_path.iterdir()] == ["a.md"]
