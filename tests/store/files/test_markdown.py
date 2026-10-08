from pathlib import Path

import pytest

from tightrein.store.files.markdown import demote_headings, normalize, write_markdown


def test_written_markdown_is_lf_and_ends_with_one_newline(tmp_path: Path) -> None:
    path = tmp_path / "90-issue-pending.md"
    write_markdown(path, "# 待审核\r\n\r\n正文\r\n\r\n\r\n")
    assert path.read_bytes() == "# 待审核\n\n正文\n".encode()


def test_normalize_handles_old_mac_line_endings() -> None:
    assert normalize("a\rb") == "a\nb\n"


def test_demote_headings_moves_every_level_but_not_code() -> None:
    text = "# 结论\n\n正文 # 不是标题\n\n## 备注\n\n```bash\n# 注释\n```\n\n#不是标题\n###### 六级"
    assert demote_headings(text) == (
        "## 结论\n\n正文 # 不是标题\n\n### 备注\n\n```bash\n# 注释\n```\n\n#不是标题\n###### 六级")
    assert demote_headings("# a\n~~~\n# b\n~~~", 2) == "### a\n~~~\n# b\n~~~"
    assert demote_headings("# a", 0) == "# a"
    with pytest.raises(ValueError):
        demote_headings("# a", -1)
