"""测试库的数据库迁移：只从 diff 判断，不连数据库，不读配置与凭证。

改动的文件匹配启动计划的 migrationPaths 时，启动服务会把新的迁移应用到测试库：列出新增的迁移条目与能否撤销(方案
交接的 `migration`)，以迁移文件的哈希为前置条件交用户确认；确认后哈希仍一致才启动，迁移文件又改过就要重新确认。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.implement.check.changes import Lines
from tightrein.implement.context import ImplementContext
from tightrein.protocol.boundaries import matches_path
from tightrein.protocol.naming import parse_iso

POINT = "implement.check"
APPROVE = "approve"


@dataclass(frozen=True)
class MigrationRequest:
    files: tuple[str, ...]
    entries: tuple[str, ...]  # 新增的迁移条目(非空行)
    revert: str  # 能否撤销与撤销方式
    hash: str

    def text(self) -> str:
        listing = "\n".join(f"- {line}" for line in self.entries)
        return (f"启动改动后的服务会把下列新增的迁移应用到测试库：\n{listing}\n"
                f"迁移文件：{'、'.join(self.files)}\n{self.revert}")

    def to_json(self) -> dict[str, Any]:
        return {"files": list(self.files), "entries": list(self.entries), "revert": self.revert, "hash": self.hash}


def changed(files: Sequence[str], patterns: Sequence[str]) -> list[str]:
    return [path for path in files if any(matches_path(path, pattern) for pattern in patterns)]


def entries(lines: Mapping[str, Lines], files: Sequence[str]) -> list[str]:
    return [line.strip() for path in files for line in (lines[path].added if path in lines else ()) if line.strip()]


def files_hash(worktree: Path, files: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8") + b"\0")
        target = worktree / path
        digest.update(target.read_bytes() if target.is_file() else b"")
    return digest.hexdigest()


def request(worktree: Path, lines: Mapping[str, Lines], files: Sequence[str],
            design: Mapping[str, Any]) -> MigrationRequest:
    migration = design.get("migration")
    if isinstance(migration, Mapping) and "reversible" in migration:
        revert = f"{'可以' if migration['reversible'] else '不能'}撤销：{migration.get('revertMethod') or '方案没有写撤销方式'}"
    else:
        revert = "方案没有说明能否撤销"
    return MigrationRequest(tuple(files), tuple(entries(lines, files)), revert, files_hash(worktree, files))


def confirmed(context: ImplementContext, sha: str) -> bool:
    """上一次自检停在这份迁移(同一哈希)上，之后用户通过了。"""
    previous = context.last(POINT)
    if previous is None or (previous.facts.get("migration") or {}).get("hash") != sha:
        return False
    since = parse_iso(previous.created_at) if previous.created_at else None
    return any(decision.point == POINT and decision.verdict == APPROVE
               and (since is None or parse_iso(decision.at) >= since) for decision in context.decisions)
