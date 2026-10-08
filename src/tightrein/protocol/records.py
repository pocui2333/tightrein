"""记录(protocol/records.md)：每次运行的事件日志 events.jsonl，与交接中记下的版本。

- 只记关键事件(运行与每一步的开始结束、写操作、到关卡、失败与停下)，量化数据不重复，引用 handoff.json；
- 每行以追加模式用一次 os.write 写入：多个进程同写一个文件时行与行不交错；
- 写入失败不抛出，原因记入 failures，由运行摘要报告：日志写不了不该让正在进行的修复停下；
- 只对一句话(summary)脱敏；编号、时间、调用点、引用(交接文件路径、幂等键)不处理，
  避免把编号、哈希中的数字串误判成手机号等脱掉。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tightrein.protocol.handoff import Versions
from tightrein.protocol.naming import Clock, format_iso
from tightrein.protocol.security import Redactor

KINDS = frozenset({"trigger", "decision", "action", "effect"})
FILE_MODE = 0o644


@dataclass(frozen=True)
class WriteFailure:
    path: Path
    reason: str


class EventLog:
    def __init__(self, path: Path, redactor: Redactor, clock: Clock) -> None:
        self.path = path
        self.redactor = redactor
        self.clock = clock
        self.failures: list[WriteFailure] = []

    def emit(self, *, run: str, subject: str | None, point: str, kind: str, summary: str,
             refs: Mapping[str, str] = {}) -> None:
        if kind not in KINDS:
            raise ValueError(f"事件种类只能是 {', '.join(sorted(KINDS))}：{kind}")
        event = {
            "at": format_iso(self.clock.now()),
            "run": run,
            "subject": subject,
            "point": point,
            "kind": kind,
            "summary": self.redactor.text(summary),
            "refs": dict(refs),
        }
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
            try:
                written = os.write(descriptor, line)
            finally:
                os.close(descriptor)
            if written != len(line):
                raise OSError(f"只写入了 {written} / {len(line)} 字节")
        except OSError as error:
            self.failures.append(WriteFailure(self.path, f"{type(error).__name__}: {error}"))


def versions(tool_root: Path, settings_hash: str, *, prompt_hash: str | None, tool: str | None,
             tool_version: str | None, model: str | None) -> Versions:
    return Versions(tightrein=_head_commit(tool_root), prompt=prompt_hash, settings=settings_hash, tool=tool,
                    tool_version=tool_version, model=model)


def _head_commit(root: Path) -> str | None:
    """tightrein 自身的 commit：直接读 .git，不起 git 子进程(每份交接都要取一次)。不是 git 仓库时为 None。"""
    try:
        git_dir = _git_dir(root)
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head.removeprefix("ref: ")
        common = _common_dir(git_dir)
        for directory in (git_dir, common):
            loose = directory / ref
            if loose.is_file():
                return loose.read_text(encoding="utf-8").strip()
        packed = common / "packed-refs"
        for line in packed.read_text(encoding="utf-8").splitlines():
            commit, _, name = line.partition(" ")
            if name == ref:
                return commit
    except OSError:
        return None
    return None


def _git_dir(root: Path) -> Path:
    """worktree 里 .git 是一个写着 `gitdir: <路径>` 的文件。"""
    marker = root / ".git"
    if marker.is_file():
        return (root / marker.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")).resolve()
    return marker


def _common_dir(git_dir: Path) -> Path:
    """worktree 的分支引用与 packed-refs 在主仓库的 .git 里，路径记在 commondir。"""
    pointer = git_dir / "commondir"
    if pointer.is_file():
        return (git_dir / pointer.read_text(encoding="utf-8").strip()).resolve()
    return git_dir
