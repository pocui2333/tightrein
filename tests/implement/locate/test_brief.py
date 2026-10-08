"""定位与代码笔记之间的一层：位置核对与补全(与评估共用一份)、笔记够不够用。"""

from __future__ import annotations

from typing import Any

from tightrein.assess.notes import CORE, RELATED, CodeNotes, NoteEntry
from tightrein.implement.locate import brief


def test_locations_are_checked_against_the_worktree(world: Any) -> None:
    root = world.repo.path
    assert brief.location_problem(root, "src/orders.py:1-2") is None
    assert brief.location_problem(root, "`src/orders.py:2`") is None
    assert brief.location_problem(root, {"file": "src/orders.py", "line": 1}) is None
    assert "不存在" in (brief.location_problem(root, "src/missing.py:1") or "")
    (world.tmp / "outside.py").write_text("X = 1\n", encoding="utf-8")
    assert "不存在" in (brief.location_problem(root, "../outside.py:1") or "")  # 挡住越出 worktree 的写法
    assert "只有 2 行" in (brief.location_problem(root, "src/orders.py:2-9") or "")
    assert "不是「文件路径:行号」" in (brief.location_problem(root, "src/orders.py") or "")


def test_bare_file_names_are_completed_by_a_unique_suffix(world: Any) -> None:
    world.repo.write({"lib/users.py": "X = 1\n"})
    output = {"core": [{"location": "orders.py:2", "description": "只取当天"}],
              "related": [{"location": "users.py:1", "description": "两处同名，不补"}],
              "analysis": "orders.py:1 不动"}
    done = brief.complete(output, world.repo.path)
    assert [item["location"] for item in done["core"]] == ["src/orders.py:2"]
    assert done["related"][0]["location"] == "users.py:1"
    assert done["analysis"] == "orders.py:1 不动"  # 只处理写位置的键
    assert brief.check(done, world.repo.path) == ["位置不存在或越界：users.py 在代码中不存在"]


def test_findings_keep_the_roles_and_files_are_listed_once(world: Any) -> None:
    output = {"core": [{"location": "src/orders.py:2", "description": "只取当天"}],
              "related": [{"location": "src/orders.py:1", "description": "入口"},
                          {"location": "tests/test_orders.py:4", "description": "已有测试"}]}
    found = brief.findings(output)
    assert [item["role"] for item in found] == [CORE, RELATED, RELATED]
    assert brief.files(found) == ["src/orders.py", "tests/test_orders.py"]
    assert brief.check(output, world.repo.path) == []


def test_notes_are_enough_only_with_core_locations_that_still_hold(world: Any) -> None:
    root = world.repo.path
    assert not brief.sufficient(None, root)
    related_only = CodeNotes("0018", world.repo.base, [NoteEntry("src/orders.py:1", RELATED, "入口")])
    assert not brief.sufficient(related_only, root)
    good = CodeNotes("0018", world.repo.base, [NoteEntry("src/orders.py:2", CORE, "只取当天")])
    assert brief.sufficient(good, root) and brief.missing_core(good, root) == []
    stale = CodeNotes("0018", world.repo.base, [NoteEntry("src/orders.py:2", CORE, "只取当天"),
                                                 NoteEntry("src/orders.py:30", CORE, "代码变了")])
    assert not brief.sufficient(stale, root)
    assert brief.missing_core(stale, root) == ["src/orders.py:30"]
