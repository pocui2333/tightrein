"""解析 runtime/signal-reporter.ts 写出的 results.ndjson(architecture/04 3.5)。

每行一条用例在最终一次尝试后的结果：title、file、project、role、tags、outcome(expected、unexpected、flaky、
skipped)、failedStep、error、attachments(screenshot、trace、video 的路径与各次尝试的 observations)、startedAt、
durationMs、retries。错误消息中的终端颜色控制符在这里去掉。
结果文件缺失、为空或最后一行不是完整的 JSON 时 complete 为假；中间无法解析的行跳过并计数。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.domain.clock import parse_iso
from tightrein.pipeline.checks.pages.plan import SETUP_PREFIX

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
OUTCOMES = frozenset({"expected", "unexpected", "flaky", "skipped"})


@dataclass(frozen=True)
class CaseResult:
    title: str
    file: str
    project: str
    role: str
    outcome: str
    failed_step: str | None
    error: str | None
    tags: tuple[str, ...] = ()
    screenshots: tuple[str, ...] = ()
    traces: tuple[str, ...] = ()
    videos: tuple[str, ...] = ()
    observations: tuple[Mapping[str, Any], ...] = ()
    started_at: datetime | None = None
    duration_ms: int | None = None
    retries: int = 1

    @property
    def setup(self) -> bool:
        return self.project.startswith(SETUP_PREFIX)


@dataclass(frozen=True)
class Results:
    cases: tuple[CaseResult, ...]
    complete: bool
    unknown: int = 0


def _case(data: Mapping[str, Any]) -> CaseResult:
    attachments = data.get("attachments") or {}
    outcome = data["outcome"]
    if outcome not in OUTCOMES:
        raise ValueError(f"未知的 outcome：{outcome}")
    error = data.get("error")
    started = data.get("startedAt")
    return CaseResult(
        title=data["title"], file=data["file"], project=data["project"], role=data["role"], outcome=outcome,
        failed_step=data.get("failedStep"), error=None if error is None else ANSI_ESCAPE.sub("", error),
        tags=tuple(data.get("tags") or ()), screenshots=tuple(attachments.get("screenshot") or ()),
        traces=tuple(attachments.get("trace") or ()), videos=tuple(attachments.get("video") or ()),
        observations=tuple(attachments.get("observations") or ()),
        started_at=None if started is None else parse_iso(started), duration_ms=data.get("durationMs"),
        retries=data.get("retries") or 1,
    )


def parse(path: Path) -> Results:
    if not path.is_file():
        return Results((), False)
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    cases: list[CaseResult] = []
    unknown = 0
    complete = bool(lines)
    for index, line in enumerate(lines):
        try:
            cases.append(_case(json.loads(line)))
        except (ValueError, KeyError, TypeError, AttributeError):
            unknown += 1
            if index == len(lines) - 1:
                complete = False
    return Results(tuple(cases), complete, unknown)
