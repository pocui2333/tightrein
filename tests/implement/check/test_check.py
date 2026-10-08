from __future__ import annotations

from typing import Any

from tightrein.implement.check import check
from tightrein.implement.check.findings import LOCAL, NEEDS_USER
from tightrein.protocol.handoff import Status

FIX = {"src/orders.py": "def recent(days):\n    return days * 7\n",
       "tests/test_orders.py": "from src.orders import recent\n\n\ndef test_recent():\n    assert recent(1) == 7\n"}
DESIGN = {"files": [{"path": "src/orders.py", "isNew": False, "reason": None},
                    {"path": "tests/test_orders.py", "isNew": False, "reason": None}]}


def _context(world: Any, **options: Any) -> Any:
    latest = {"implement.design": world.handoff("implement.design", DESIGN), **options.pop("latest", {})}
    return world.context(latest=latest, **options)


def test_a_clean_change_passes_with_full_checks(world: Any) -> None:
    world.repo.write(FIX)
    world.respond(("pytest",), {"stdout": "tests/test_orders.py::test_recent PASSED\n"})
    world.respond(("ruff",), {"stdout": "All checks passed!\n"})
    result = check.run(world.runtime, _context(world))
    assert result.status is Status.PASSED and result.point == "implement.check"
    facts = result.facts
    assert facts["scope"] == "full" and facts["blockers"] == [] and facts["runtime"] == []
    assert facts["diffHash"] == world.git().diff_hash(world.repo.base)
    assert sorted(item["command"] for item in facts["commands"]) == ["pytest -q", "ruff check ."]
    assert set(facts["fileStates"]) == {"src/orders.py", "tests/test_orders.py"}
    assert result.metrics.passed == 2 and result.metrics.failed == 0 and result.metrics.files_changed == 2


def test_failures_are_local_problems_and_unrunnable_checks_need_the_user(world: Any) -> None:
    world.repo.write(FIX)
    world.respond(("pytest",), {"exit_code": 1, "stdout": "FAILED tests/test_orders.py::test_recent\nE   assert 1 == 7\n"})
    world.respond(("ruff",), {"exit_code": None, "start_error": "ruff: command not found"})
    result = check.run(world.runtime, _context(world))
    assert result.status is Status.FAILED
    blockers = {item["kind"]: item for item in result.facts["blockers"]}
    assert blockers["failed"]["category"] == LOCAL and blockers["failed"]["location"] == "test"
    # 没跑起来既不算通过也不交回编码：需要用户
    assert blockers["not_run"]["category"] == NEEDS_USER and result.facts["firstCategory"] == NEEDS_USER
    output = next(item["output"] for item in result.facts["commands"] if item["name"] == "test")
    assert "E   assert 1 == 7" in output


def test_correction_rounds_run_affected_tests_first_then_everything(world: Any) -> None:
    world.repo.write(FIX)
    world.respond(("pytest",), {"exit_code": 1, "stdout": "FAILED x\n"})
    world.respond(("ruff",), {})
    first = check.run(world.runtime, _context(world))
    assert first.status is Status.FAILED
    world.repo.write({"src/orders.py": "def recent(days):\n    return days * 7  # 7 天\n"})
    world.runner.commands.clear()
    world.respond(("pytest",), {"exit_code": 1, "stdout": "FAILED again\n"})
    second = check.run(world.runtime, _context(world, round=2, latest={"implement.check": first}))
    # 只跑了受影响的测试，有失败就不再补跑全量(同一批命令并行，先后不定)
    assert sorted(command.argv for command in world.runner.others()) == [("pytest", "-q", "tests/test_orders.py"),
                                                                        ("ruff", "check", ".")]
    assert second.facts["scope"] == "affected" and second.facts["changedThisRound"] == ["src/orders.py"]
    world.repo.write({"src/orders.py": "def recent(days):\n    return days * 7  # 一周\n"})
    world.runner.commands.clear()
    world.respond(("pytest",), {})
    third = check.run(world.runtime, _context(world, round=3, latest={"implement.check": second}))
    assert third.status is Status.PASSED and third.facts["scope"] == "full"
    argvs = [command.argv for command in world.runner.others()]
    assert ("pytest", "-q", "tests/test_orders.py") in argvs and ("pytest", "-q") in argvs


def test_an_unchanged_diff_reuses_the_passed_result(world: Any) -> None:
    world.repo.write(FIX)
    world.respond(("pytest",), {})
    world.respond(("ruff",), {})
    first = check.run(world.runtime, _context(world))
    world.runner.commands.clear()
    again = check.run(world.runtime, _context(world, round=2, latest={"implement.check": first}))
    assert again.status is Status.PASSED and again.summary.startswith("改动没变") and world.runner.others() == []


def test_checks_that_modify_the_worktree_are_local_problems(world: Any) -> None:
    world.repo.write(FIX)

    def reformat(command: Any) -> None:
        (world.repo.path / "src/orders.py").write_text("def recent(days):\n    return 7 * days\n")

    world.respond(("pytest",), {})
    world.respond(("ruff",), {"effect": reformat})
    result = check.run(world.runtime, _context(world))
    assert [item["kind"] for item in result.facts["blockers"]] == ["worktree_modified"]


def test_oversized_changes_converge_once_then_stop(new_world: Any, settings_with: Any) -> None:
    world = new_world(settings=settings_with({"boundaries": {"changeCap": {"files": 1, "lines": 400}}},
                                             commands={}))
    world.repo.write({"src/orders.py": "A = 1\n", "src/users.py": "B = 2\n"})
    design = {"implement.design": world.handoff("implement.design", {"files": ["src/orders.py", "src/users.py"]})}
    first = check.run(world.runtime, world.context(latest=design))
    assert [(item["kind"], item["category"]) for item in first.facts["blockers"]] == [("over_cap", LOCAL)]
    world.repo.write({"src/users.py": "B = 3\n"})
    second = check.run(world.runtime, world.context(round=2, latest={**design, "implement.check": first}))
    assert [(item["kind"], item["category"]) for item in second.facts["blockers"]] == [("over_cap", NEEDS_USER)]
