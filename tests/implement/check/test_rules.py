from __future__ import annotations

import re
from typing import Any

from tightrein.implement.check import rules
from tightrein.implement.check.changes import collect
from tightrein.implement.check.findings import LOCAL, NEEDS_USER, PLAN_GAP
from tightrein.implement.check.rules import RuleSettings

RULES = RuleSettings(skip_markers=("pytest.mark.skip", ".skip("), residue=(re.compile(r"print\(.*DEBUG"),),
                     min_literal_length=4)


def _evaluate(world: Any, planned: list[str], *, over_cap_before: bool = False) -> list[Any]:
    changes = collect(world.git(), world.repo.base)
    return rules.evaluate(changes, planned=planned, worktree=world.repo.path, settings=world.runtime.settings,
                          rules=RULES, over_cap_before=over_cap_before)


def test_a_clean_change_passes(world: Any) -> None:
    world.repo.write({"src/orders.py": "def recent(days):\n    return days * 7\n",
                      "tests/test_recent.py": "def test_week():\n    assert True\n"})
    assert _evaluate(world, ["src/orders.py"]) == []


def test_each_rule_has_its_category(world: Any) -> None:
    world.repo.write({
        "src/orders.py": "def recent(days):\n    print('DEBUG', days)\n    return days\n",
        "src/users.py": "NAME = 'b'\n",
        "scratch.txt": "notes\n",
        "tests/test_orders.py": "import pytest\n\n\n@pytest.mark.skip\ndef test_recent():\n    assert True\n",
        ".env": "TOKEN=x\n",
    })
    found = {(item.kind, item.location): item.category for item in _evaluate(world, ["src/orders.py"])}
    assert found[("outside_plan", "src/users.py")] == PLAN_GAP
    assert found[("temporary_file", "scratch.txt")] == LOCAL
    assert found[("test_modified", "tests/test_orders.py")] == LOCAL
    assert found[("skip_marker", "tests/test_orders.py")] == LOCAL
    assert found[("residue", "src/orders.py")] == LOCAL
    assert found[("forbidden", ".env")] == NEEDS_USER


def test_lines_with_protected_markers_need_the_user_unless_confirmed(world: Any) -> None:
    world.repo.write({"src/orders.py": "# @generated 不要手改\ndef recent(days):\n    return days * 7\n"})
    marked = RuleSettings(skip_markers=(), residue=(), min_literal_length=4, protected_markers=("@generated",))
    changes = collect(world.git(), world.repo.base)
    found = rules.evaluate(changes, planned=["src/orders.py"], worktree=world.repo.path,
                           settings=world.runtime.settings, rules=marked, over_cap_before=False)
    assert [(item.kind, item.location, item.category, item.summary) for item in found] == [
        ("protected_content", "src/orders.py", NEEDS_USER, "新增了受保护标记 @generated")]
    assert rules.evaluate(changes, planned=["src/orders.py"], worktree=world.repo.path, settings=world.runtime.settings,
                          rules=marked, over_cap_before=False, approved=["src/orders.py"]) == []
    assert rules.approved_files({"protectedTouches": [{"path": "src/orders.py", "change": "改生成代码"}]}) == [
        "src/orders.py"]


def test_oversized_changes_converge_once_and_then_go_to_the_user(world: Any, settings_with: Any,
                                                                 new_world: Any) -> None:
    small = new_world(settings=settings_with({"boundaries": {"changeCap": {"files": 1, "lines": 400},
                                                             "autoApprove": {"files": 1, "lines": 100}}}))
    small.repo.write({"src/orders.py": "A = 1\n", "src/users.py": "B = 2\n",
                      "tests/test_more.py": "X = 1\n" * 50})  # 测试文件不计入
    first = [item for item in _evaluate(small, []) if item.kind == "over_cap"]
    assert len(first) == 1 and first[0].category == LOCAL and rules.SIZE_GUIDANCE in first[0].summary
    again = [item for item in _evaluate(small, [], over_cap_before=True) if item.kind == "over_cap"]
    assert again[0].category == NEEDS_USER


def test_without_a_plan_nothing_is_outside_it(world: Any) -> None:
    world.repo.write({"src/users.py": "NAME = 'b'\n"})
    assert _evaluate(world, []) == []


def test_suspected_hardcode_is_only_a_hint(world: Any) -> None:
    world.repo.write({"src/orders.py": "def recent(days):\n    if days == 'order-77':\n        return 1\n    return 0\n",
                      "tests/test_recent.py": "def test_x():\n    assert recent('order-77') == 1\n"})
    changes = collect(world.git(), world.repo.base)
    hints = rules.hardcode_hints(changes, test_patterns=("tests/",), issue_text="复现：传入 12345 时报错",
                                 min_length=4)
    assert [(hint.path, hint.literal) for hint in hints] == [("src/orders.py", "order-77")]
    assert rules.literals("x = 'id' + \"long-value\" + 7 + 12345", 4) == {"long-value", "12345"}


def test_planned_files_accept_paths_or_objects() -> None:
    assert rules.planned_files({"files": ["a.py", {"path": "b.py", "isNew": True}, {"x": 1}]}) == ["a.py", "b.py"]
    assert rules.planned_files({}) == []
