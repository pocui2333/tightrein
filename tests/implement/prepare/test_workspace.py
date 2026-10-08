"""准备：分支名、建 worktree 与基准检查；worktree 的检查点快照与恢复。"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tightrein.implement.prepare import workspace
from tightrein.protocol.git.format import BranchRejected
from tightrein.protocol.handoff import Status
from tightrein.protocol.recovery import CHECKPOINT_COMMIT
from tightrein.store.tables import issues


def _with_origin(world: Any) -> None:
    origin = world.tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    world.repo.git("remote", "add", "origin", str(origin))
    world.repo.git("push", "-q", "-u", "origin", "main")


def _issue(world: Any, **changes: Any) -> Any:
    found = issues.get(world.runtime.conn, "0018")
    return replace(found, **changes)


def test_snapshot_records_changes_without_touching_head_and_restore_brings_them_back(world: Any) -> None:
    git = world.git()
    head = git.head().commit
    assert workspace.snapshot(git) == head  # 干净时就是 HEAD
    world.repo.write({"src/orders.py": "def recent(days):\n    return days * 7\n", "src/new.py": "X = 1\n"})
    snap = workspace.snapshot(git)
    assert snap != head and git.head().commit == head and git.status().changed_paths == ("src/new.py", "src/orders.py")
    assert workspace.current_tree(git) == workspace.tree_of(git, snap)
    world.repo.write({"src/orders.py": "broken\n", "src/later.py": "Y = 2\n", "README.md": None})
    workspace.restore(git, snap)
    assert (world.repo.path / "src/orders.py").read_text() == "def recent(days):\n    return days * 7\n"
    assert (world.repo.path / "src/new.py").read_text() == "X = 1\n"
    assert not (world.repo.path / "src/later.py").exists() and (world.repo.path / "README.md").exists()
    # 新文件恢复后仍是未跟踪的，HEAD 与暂存区不动
    assert "src/new.py" in git.untracked() and git.head().commit == head
    assert world.repo.git("diff", "--cached", "--name-only") == ""
    workspace.restore(git, head)
    assert git.status().clean


def test_branch_names_follow_the_conventions(world: Any) -> None:
    path = world.tmp / "absent"
    assert workspace._branch(world.runtime, _issue(world), path) == "fix/18-orders"  # Issue 记下的分支
    fresh = _issue(world, branch=None, title="Orders list shows only today")
    assert workspace._branch(world.runtime, fresh, path) == "fix/18-orders-list-shows-only-today"
    world.repo.git("branch", "fix/18-orders-list-shows-only-today")
    assert workspace._branch(world.runtime, fresh, path) == "fix/18-orders-list-shows-only-today-2"
    urgent = _issue(world, branch=None, title="订单", severity="P0", extra={"slug": "orders"})
    assert workspace._branch(world.runtime, urgent, path) == "hotfix/18-orders"
    assert workspace._branch(world.runtime, _issue(world, branch=None, title="订单"), path) == "fix/18-bug"


def test_a_later_attempt_gets_its_own_worktree_and_an_unused_branch(world: Any) -> None:
    """回归、重开后的又一次修复(issues.extra.attempt)：新的修复目录；分支名被上一次占着时依次加 -2、-3。"""
    first = _issue(world, branch=None, title="Orders list shows only today")
    assert workspace.worktree_path(world.runtime, first) == world.runtime.workspace.worktree("0018")
    second = _issue(world, branch=None, title="Orders list shows only today", extra={"attempt": 2})
    assert workspace.worktree_path(world.runtime, second) == world.runtime.workspace.worktree("0018_2")
    path = world.tmp / "absent"
    world.repo.git("branch", "fix/18-orders-list-shows-only-today")
    world.repo.git("branch", "fix/18-orders-list-shows-only-today-2")
    assert workspace._branch(world.runtime, second, path) == "fix/18-orders-list-shows-only-today-3"


def test_an_ai_prefix_is_rejected(new_world: Any, settings_with: Any) -> None:
    world = new_world(settings=settings_with({"git": {"branchPrefix": "claude"}}))
    issue = _issue(world, branch=None, title="orders")
    with pytest.raises(BranchRejected):
        workspace._branch(world.runtime, issue, world.tmp / "absent")
    issues.save(world.runtime.conn, issue, world.clock)
    result = workspace.run(world.runtime, world.context())
    assert result.status is Status.FAILED and result.facts["reason"] == "branch_rejected"


def test_prepare_creates_the_worktree_and_checks_the_base_once_per_commit(world: Any) -> None:
    _with_origin(world)
    issues.save(world.runtime.conn, _issue(world, branch=None, title="orders today"), world.clock)
    world.respond(("pytest",), {"stdout": "1 passed\n"})
    world.respond(("ruff",), {"stdout": "All checks passed!\n"})
    result = workspace.run(world.runtime, world.context())
    path = world.runtime.workspace.worktree("0018")
    assert result.status is Status.PASSED and result.facts["worktree"] == str(path)
    assert result.facts["branch"] == "fix/18-orders-today" and path.is_dir()
    assert result.facts["baseCommit"] == result.facts[CHECKPOINT_COMMIT] == world.repo.base
    assert result.facts["baseChecks"]["passed"] and not result.facts["baseChecks"]["cached"]
    # 测试命令里的 {tests} 在基准上去掉，跑全量
    assert [command.argv for command in world.runner.others()] == [("pytest", "-q"), ("ruff", "check", ".")]
    world.runner.commands.clear()
    again = workspace.run(world.runtime, world.context())
    assert again.status is Status.PASSED and again.facts["baseChecks"]["cached"] and world.runner.others() == []


def test_a_failing_base_is_a_configuration_error(world: Any) -> None:
    _with_origin(world)
    world.respond(("pytest",), {"exit_code": 1, "stdout": "1 failed\n"})
    world.respond(("ruff",), {"exit_code": None, "start_error": "ruff: not found"})
    result = workspace.run(world.runtime, world.context())
    assert result.status is Status.FAILED and result.facts["reason"] == "config_error"
    assert "退出码 1" in result.summary and "起不来" in result.summary
    # 起不来的命令没缓存：修好环境后会重跑
    assert not any(Path(world.runtime.workspace.cache_dir).rglob("*.json"))


def test_a_failing_prepare_command_is_a_configuration_error(new_world: Any, settings_with: Any) -> None:
    world = new_world(settings=settings_with(commands={"prepare": "npm ci", "test": "pytest -q {tests}"}))
    _with_origin(world)
    world.respond(("npm",), {"exit_code": 1, "stdout": "npm ERR! missing lockfile\n"})
    result = workspace.run(world.runtime, world.context())
    assert result.status is Status.FAILED and result.facts["reason"] == "config_error"
    assert result.summary.startswith("准备命令失败，判为配置错误：prepare：`npm ci`")
    assert result.facts["prepareCommands"][0]["name"] == "prepare" and "baseChecks" not in result.facts
    # 准备失败就不在基准上跑检查，免得把环境问题算成基准失败
    assert [command.argv for command in world.runner.others()] == [("npm", "ci")]
