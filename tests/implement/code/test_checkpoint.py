"""检查点与回退：记下最好的一轮，变差时恢复；之后才改的文件恢复为基准内容；路径不得越出 worktree。"""

from __future__ import annotations

from typing import Any

import pytest

from tightrein.implement.code.checkpoint import Checkpoint

GOOD = {"src/orders.py": "def recent(days):\n    return days * 7\n", "src/new.py": "X = 1\n"}


def _checkpoint(world: Any, base: str | None = None) -> Checkpoint:
    return Checkpoint(world.git(), world.repo.path, base or world.repo.base, world.tmp / "checkpoint")


def test_checkpoint_restores_saved_files_and_reverts_later_changes(world: Any) -> None:
    world.repo.write(GOOD)
    point = _checkpoint(world)
    point.save(1, ["src/new.py", "src/orders.py"], findings=1)
    world.repo.write({"src/orders.py": "broken\n", "src/new.py": None, "src/users.py": "NAME = 'b'\n",
                      "src/later.py": "Y = 2\n"})
    note = point.rollback(["src/orders.py", "src/new.py", "src/users.py", "src/later.py"], "第 2 轮测试重新失败")
    assert (world.repo.path / "src/orders.py").read_text() == GOOD["src/orders.py"]
    assert (world.repo.path / "src/new.py").read_text() == "X = 1\n"
    assert (world.repo.path / "src/users.py").read_text() == "NAME = 'a'\n"  # 检查点之后才改的：恢复为基准
    assert not (world.repo.path / "src/later.py").exists()  # 检查点之后新建的：删掉
    assert "恢复到第 1 轮的检查点" in note and "第 2 轮测试重新失败" in note and "不要重复被撤销的做法" in note


def test_checkpoint_decisions_and_persistence(world: Any) -> None:
    point = _checkpoint(world)
    assert not point.exists and point.should_save(tests_passed=True, findings=3)
    assert not point.should_save(tests_passed=False, findings=0)
    assert not point.should_rollback(tests_passed=False, findings=5, threshold=3)  # 没有检查点不回退
    point.save(2, [], findings=1)
    again = _checkpoint(world)  # 换一个实例也读得到
    assert again.exists and again.round == 2
    assert again.should_save(tests_passed=True, findings=1) and not again.should_save(tests_passed=True, findings=2)
    assert again.should_rollback(tests_passed=False, findings=2, threshold=3)  # 测试重新失败
    assert again.should_rollback(tests_passed=True, findings=3, threshold=3)  # 不通过项到阈值且多于检查点
    assert not again.should_rollback(tests_passed=True, findings=2, threshold=3)
    assert not again.should_rollback(tests_passed=False, findings=1, threshold=3)  # 不比检查点差
    # 基准 commit 变了检查点作废
    assert not _checkpoint(world, base="0" * 40).exists
    again.clear()
    assert not _checkpoint(world).exists


def test_checkpoint_rejects_paths_outside_the_worktree(world: Any) -> None:
    point = _checkpoint(world)
    with pytest.raises(ValueError):
        point.save(1, ["../outside.py"], findings=0)
    with pytest.raises(ValueError):
        point.save(1, ["/etc/passwd"], findings=0)
