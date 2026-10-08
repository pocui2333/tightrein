"""编码的最好状态检查点与回退。

某一轮编码写的测试与受影响的测试都通过、且不通过项不多于已有检查点时，把相对基准改动过的文件复制到对象目录的
`checkpoint/` 记为检查点；之后某一轮测试重新失败，或不通过项达到 controls.implement.code.checkpointRollbackFindings
且多于检查点时，把这些文件恢复到检查点，检查点之后才改的文件恢复为基准内容，并把「已恢复到第 N 轮、不要重复被撤销
的做法」放在下一轮修正说明的最前面。最后一轮之后不再编码，也就不回退，worktree 保留那一轮的改动供人查看。
基准 commit 变了检查点作废；路径不得越出 worktree；只动文件，不做 git 写操作。
"""

from __future__ import annotations

import shutil
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.protocol.git import Git
from tightrein.store.files.json import read_json, write_json

MANIFEST = "checkpoint.json"
FILES = "files"


class Checkpoint:
    def __init__(self, git: Git, worktree: Path, base_commit: str, directory: Path) -> None:
        self._git = git
        self._worktree = worktree
        self._base = base_commit
        self._directory = directory
        self._saved = self._load()

    @property
    def exists(self) -> bool:
        return self._saved is not None

    @property
    def round(self) -> int | None:
        return None if self._saved is None else int(self._saved["round"])

    def clear(self) -> None:
        if self._directory.exists():
            shutil.rmtree(self._directory)
        self._saved = None

    def should_save(self, tests_passed: bool, findings: int) -> bool:
        return tests_passed and (self._saved is None or findings <= self._saved["findings"])

    def should_rollback(self, tests_passed: bool, findings: int, threshold: int) -> bool:
        if self._saved is None or findings <= self._saved["findings"]:
            return False
        return not tests_passed or findings >= threshold

    def save(self, number: int, changed: Collection[str], findings: int) -> None:
        """复制改动过的文件；worktree 中已删除的记为删除。"""
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
        write_json(self._directory / MANIFEST, self._saved)

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
            original = None if path in deleted else self._git.show(self._base, path)
            if original is None:
                target.unlink(missing_ok=True)
            else:
                # 原样写回(不补末尾换行)：原子写会改动没有末尾换行的文件
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(original, encoding="utf-8", newline="")
        return (f"上一轮的改动已撤销，worktree 已恢复到第 {saved['round']} 轮的检查点(当时测试通过，不通过 "
                f"{saved['findings']} 项)。撤销原因：{reason}。在检查点的代码上继续修改，不要重复被撤销的做法")

    def _inside(self, path: str) -> Path:
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"改动路径不在 worktree 内：{path}")
        return self._worktree.joinpath(*relative.parts)

    def _load(self) -> dict[str, Any] | None:
        path = self._directory / MANIFEST
        if not path.is_file():
            return None
        saved = read_json(path)
        return saved if saved.get("baseCommit") == self._base else None
