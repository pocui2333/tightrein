"""写代码的最佳状态检查点(38-external-techniques.md 第 3 项)。

某一轮复现检查全部通过、且不通过项不多于已有检查点时，把相对基准 commit 改动过的文件复制到修复目录下的
checkpoint/ 记为检查点；之后的轮次复现检查重新失败，或不通过项达到 thresholds.fix.checkpointRollbackFindings
且多于检查点时，把 worktree 中的这些文件恢复到检查点，检查点之后才改动的文件恢复为基准版本的内容。
只读写修复专用 worktree 中的文件与修复目录，不做 git 写操作，改动的提交照常由发布阶段负责。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.store.files import atomic
from tightrein.vcs.git_read import GitReader

MANIFEST = "checkpoint.json"
FILES = "files"


class Checkpoint:
    def __init__(self, git: GitReader, worktree: Path, base_commit: str, directory: Path) -> None:
        self._git = git
        self._worktree = worktree
        self._base = base_commit
        self._directory = directory
        self._saved = self._load()

    @property
    def exists(self) -> bool:
        return self._saved is not None

    def clear(self) -> None:
        if self._directory.exists():
            shutil.rmtree(self._directory)
        self._saved = None

    def should_save(self, repro_passed: bool, findings: int) -> bool:
        return repro_passed and (self._saved is None or findings <= self._saved["findings"])

    def should_rollback(self, repro_passed: bool, findings: int, threshold: int) -> bool:
        if self._saved is None or findings <= self._saved["findings"]:
            return False
        return not repro_passed or findings >= threshold

    def save(self, number: int, changed: Collection[str], findings: int) -> None:
        """记检查点：复制改动过的文件，worktree 中已删除的记为删除。"""
        self.clear()
        present, deleted = [], []
        for path in sorted(changed):
            source = self._inside(path)
            if source.is_file():
                target = self._directory / FILES / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                present.append(path)
            else:
                deleted.append(path)
        self._saved = {"baseCommit": self._base, "round": number, "findings": findings,
                       "files": present, "deleted": deleted}
        atomic.write_text(self._directory / MANIFEST, json.dumps(self._saved, ensure_ascii=False, indent=2) + "\n")

    def rollback(self, changed: Collection[str], reason: str) -> str:
        """恢复到检查点，返回交给下一轮的说明。"""
        saved = self._saved
        if saved is None:
            raise RuntimeError("没有检查点可退回")
        present, deleted = set(saved["files"]), set(saved["deleted"])
        for path in sorted(set(changed) | present | deleted):
            target = self._inside(path)
            if path in present:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._directory / FILES / path, target)
                continue
            original = None if path in deleted else self._git.show(self._worktree, self._base, path)
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(original, encoding="utf-8")
        return (f"本轮改动已撤销，worktree 已恢复到第 {saved['round']} 轮的检查点(当时复现检查通过，"
                f"不通过 {saved['findings']} 项)。退回原因：{reason}。在检查点的代码上继续修改，不要重复被撤销的做法")

    def _inside(self, path: str) -> Path:
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"改动路径不在 worktree 内：{path}")
        return self._worktree.joinpath(*relative.parts)

    def _load(self) -> dict[str, Any] | None:
        path = self._directory / MANIFEST
        if not path.is_file():
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
        return saved if saved.get("baseCommit") == self._base else None
