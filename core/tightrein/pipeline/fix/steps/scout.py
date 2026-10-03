"""勘察(architecture/07 4.4)：调用 fix-scout，代码检查每处位置在 worktree 中真实存在。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.domain.enums import RunnerStatus
from tightrein.evaluation.scorers.code import location_problem

FINDING_GROUPS = ("existing", "reusable", "dataStructure", "linkage", "problems")


def locations(output: Mapping[str, Any]) -> list[str]:
    found = [item["location"] for group in FINDING_GROUPS for item in output.get(group) or []]
    design = output.get("designIssue")
    if design:
        found += list(design["locations"])
    return found


def check(output: Mapping[str, Any], worktree: Path) -> list[str]:
    return [problem for location in locations(output) if (problem := location_problem(worktree, location))]


def linkage_files(output: Mapping[str, Any] | None) -> list[str]:
    return [item["location"].rsplit(":", 1)[0] for item in (output or {}).get("linkage") or []]


def status_text(status: RunnerStatus, error: str | None) -> str:
    return f"执行器返回 {status.value}" + (f"({error})" if error else "")


def feedback(problems: Sequence[str]) -> list[str]:
    return [f"位置不存在或越界：{problem}" for problem in problems]
