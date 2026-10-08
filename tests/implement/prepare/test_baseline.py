"""基准检查：准备命令每个 worktree 都跑，项目检查按基准 commit 与命令缓存。"""

from __future__ import annotations

from typing import Any

from tightrein.implement.prepare import baseline


def test_checks_are_cached_per_commit_and_commands(new_world: Any, settings_with: Any) -> None:
    world = new_world(settings=settings_with(commands={"test": "pytest -q {tests}", "prepare": "npm ci"}))
    log = world.tmp / "log.log"
    world.respond(("npm",), {"stdout": "added 3 packages\n"})
    world.respond(("pytest",), {"stdout": "ok\n"})
    assert [run.result for run in baseline.prepare(world.runtime, world.repo.path, log)] == [baseline.PASSED]
    first = baseline.check(world.runtime, world.repo.path, "abc", log)
    assert first.passed and not first.cached and first.runs[0].command == "pytest -q"
    world.runner.commands.clear()
    assert baseline.check(world.runtime, world.repo.path, "abc", log).cached and world.runner.others() == []
    assert not baseline.check(world.runtime, world.repo.path, "def", log).cached  # 另一个基准重新跑
    assert "$ npm ci" in log.read_text() and "$ pytest -q" in log.read_text()


def test_failures_and_timeouts_are_described(world: Any) -> None:
    world.respond(("pytest",), {"exit_code": None, "stopped_by": "timeout"})
    world.respond(("ruff",), {"exit_code": 2})
    found = baseline.check(world.runtime, world.repo.path, "abc", world.tmp / "log.log")
    assert not found.passed
    assert baseline.describe(found.runs) == ["test：`pytest -q` 被终止(timeout)", "lint：`ruff check .` 退出码 2"]
