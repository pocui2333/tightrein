"""边界检查的报告与违规项(architecture/02 3.3、3.11)。

报告按 runner/guard-report.schema.json 输出。除 suspected-hardcode 外，任何违规都判为不通过；suspected-hardcode 只提示，
交 fix-reviewer 判断。报告文件同时保存本次的快照等补充信息，写入 `raw/guards/<角色>-<对象编号>.json`。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.domain.enums import ViolationKind
from tightrein.store.files import atomic

SCHEMA = "runner/guard-report.schema.json"
NON_BLOCKING = frozenset({ViolationKind.SUSPECTED_HARDCODE})


@dataclass(frozen=True, order=True)
class Violation:
    kind: ViolationKind
    path: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "path": self.path, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Violation:
        return cls(ViolationKind(data["kind"]), data["path"], data["detail"])

    def __str__(self) -> str:
        where = f" {self.path}" if self.path else ""
        return f"{self.kind.value}{where}：{self.detail}"


def blocking(violations: Iterable[Violation]) -> tuple[Violation, ...]:
    return tuple(item for item in violations if item.kind not in NON_BLOCKING)


def summary(violations: Iterable[Violation]) -> str:
    return "；".join(str(item) for item in violations)


class GuardBlocked(Exception):
    """运行前检查发现无法启动 agent 的情况(worktree 中有凭证文件、git 状态无法读取、只读锁定失败)。"""

    def __init__(self, violations: Iterable[Violation]) -> None:
        self.violations = tuple(violations)
        if not self.violations:
            raise ValueError("GuardBlocked 至少需要一条违规")
        super().__init__(summary(self.violations))


@dataclass(frozen=True)
class GuardReport:
    violations: tuple[Violation, ...] = ()
    changed_files: tuple[str, ...] = ()
    lines_added: int = 0
    lines_removed: int = 0
    removed_env_names: tuple[str, ...] = ()
    report_path: Path | None = None
    restore_error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not blocking(self.violations)

    @property
    def first_blocking(self) -> Violation | None:
        found = blocking(self.violations)
        return found[0] if found else None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "violations": [item.to_dict() for item in self.violations],
            "changedFiles": list(self.changed_files),
            "linesAdded": self.lines_added,
            "linesRemoved": self.lines_removed,
        }
        if self.removed_env_names:
            data["removedEnvNames"] = list(self.removed_env_names)
        validate.check(SCHEMA, data)
        return data


def write_report(path: Path, report: GuardReport, extra: Mapping[str, Any] | None = None) -> None:
    """报告文件：`report` 为 guard-report 结构，其余键为快照、恢复失败原因等补充信息。"""
    document: dict[str, Any] = {"report": report.to_dict(), "restoreError": report.restore_error,
                                "details": dict(report.details)}
    document.update(extra or {})
    atomic.write_text(path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
