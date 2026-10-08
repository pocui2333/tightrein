"""严重度与粗规模档。

- 严重度以取证给出的 report.severity(按「不修会怎样」并参考项目说明)为准，没有时才按影响类别映射；没有影响面
  但预估 P0 的仍记 P0。同一类问题在不同项目可以是不同级别；
- 只给小、中、大粗档用于分流(实施出方案时会重估)：取模型给的档与按预估文件数算出的档中较大的一个，门槛在
  controls.assess.sizes。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SEVERITIES = ("P0", "P1", "P2", "P3")
SIZES = ("small", "medium", "large")
SEVERITY_BY_IMPACT: dict[str, str] = {
    "authorization": "P0",
    "data-ownership": "P0",
    "data-correctness": "P0",
    "credential-leak": "P0",
    "core-flow-broken": "P1",
    "non-core-error": "P2",
    "contract-mismatch": "P2",
    "experience": "P3",
    "slow-response": "P3",
    "dependency-vulnerability": "P3",
}


@dataclass(frozen=True)
class Rating:
    severity: str | None
    size: str | None
    task_type: str | None
    impact_kind: str | None
    files: tuple[str, ...]


def rate(output: Mapping[str, Any] | None, *, p0: bool, size_limits: Mapping[str, Any]) -> Rating:
    output = output or {}
    impact = output.get("impact") or {}
    kind = impact.get("kind")
    assessed = (output.get("report") or {}).get("severity")
    severity = assessed or (SEVERITY_BY_IMPACT[kind] if kind else ("P0" if p0 else None))
    assessment = output.get("assessment")
    if assessment is None:
        files = tuple(dict.fromkeys(cause["file"] for cause in output.get("rootCauses") or []))
        return Rating(severity, None, None, kind, files)
    files = tuple(dict.fromkeys(item["path"] for item in assessment.get("files") or []))
    size = larger(assessment.get("size") or "small", size_by_files(len(files), size_limits))
    return Rating(severity, size, assessment.get("taskType"), kind, files)


def size_by_files(count: int, limits: Mapping[str, Any]) -> str:
    for size in SIZES[:-1]:
        if count <= int(limits[size]["files"]):
            return size
    return SIZES[-1]


def larger(first: str, second: str) -> str:
    return max(first, second, key=SIZES.index)


def higher(first: str | None, second: str | None) -> str | None:
    """两个严重度中更重的一个(P0 最重)。"""
    found = [item for item in (first, second) if item is not None]
    return min(found, key=SEVERITIES.index) if found else None
