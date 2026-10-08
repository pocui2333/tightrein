from __future__ import annotations

from pathlib import Path
from typing import Any

from tightrein.implement.check.changes import (
    UNTRACKED,
    changed_since,
    collect,
    file_states,
    parse_diff,
    select_files,
)


def test_collect_includes_untracked_files_and_their_lines(world: Any) -> None:
    world.repo.write({"src/orders.py": "def recent(days):\n    return days * 7\n", "src/new.py": "A = 1\nB = 2\n"})
    (world.repo.path / "src/link.py").symlink_to("orders.py")
    changes = collect(world.git(), world.repo.base)
    by_path = {change.path: change for change in changes.files}
    assert by_path["src/new.py"].status == UNTRACKED and by_path["src/new.py"].added == 2
    assert by_path["src/orders.py"].added == 1 and by_path["src/orders.py"].deleted == 1
    assert changes.lines["src/new.py"].added == ("A = 1", "B = 2")
    assert changes.lines["src/orders.py"].added == ("    return days * 7",)
    assert changes.lines["src/orders.py"].removed == ("    return days",)
    # 补丁把未跟踪的新文件以新增文件的形式附在后面，跳过符号链接
    assert "+++ b/src/new.py" in changes.patch and "new file mode 100644" in changes.patch
    assert "src/link.py" not in changes.patch
    assert changes.untracked == frozenset({"src/new.py", "src/link.py"})


def test_parse_diff_and_select_files() -> None:
    patch = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
             "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1,2 @@\n x\n+y\n")
    parsed = parse_diff(patch)
    assert parsed["a.py"].added == ("new",) and parsed["a.py"].removed == ("old",)
    assert parsed["b.py"].added == ("y",) and parsed["b.py"].removed == ()
    only_b = select_files(patch, ["b.py"])
    assert only_b.startswith("diff --git a/b.py") and "a.py" not in only_b


def test_file_states_find_what_changed_this_round(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("1\n")
    (tmp_path / "b.py").write_text("2\n")
    before = file_states(tmp_path, ["a.py", "b.py"])
    (tmp_path / "a.py").write_text("changed\n")
    (tmp_path / "b.py").unlink()
    (tmp_path / "c.py").write_text("3\n")
    after = file_states(tmp_path, ["a.py", "b.py", "c.py"])
    assert after["b.py"] == ""
    assert changed_since(before, after) == ["a.py", "b.py", "c.py"]
    assert changed_since(after, after) == []
