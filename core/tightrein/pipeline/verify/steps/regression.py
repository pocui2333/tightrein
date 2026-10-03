"""合并前验证第 4 步中的接口浅跑与页面巡检(architecture/07 13)：经注入的 api-fuzz 探针与页面运行器执行。

接口浅跑以 include_paths 限定到受影响接口的路由；与基准比对：existing 给出信号对应的、修复前就已存在的问题编号
(按指纹比对)，这类信号只在原因中列出，不计失败，只有本次新出现的信号计为失败。页面巡检运行工作区的巡检用例，
受影响页面上出现的失败(用例失败、控制台报错、失败请求)计为失败，其余页面不影响本次验证。探针或运行器没有提供时
记为未验证；它们本身出错(不是检查失败)记为未验证并附原因。截图取巡检产物中的全部图片(路径相对原始输出目录)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from typing import Any

from tightrein.config import normalize as normalize_rules

from tightrein.domain.enums import CheckResult, ProbeLevel, ProblemStatus, RunStatus
from tightrein.domain.fingerprint import CURRENT_VERSION, fingerprint
from tightrein.domain.normalize import location as normalize_location
from tightrein.domain.normalize import normalize
from tightrein.domain.signal import Signal
from tightrein.pipeline.verify.steps.verdict import API_SHALLOW, PAGE_PATROL, Item
from tightrein.sources.base import ProbeOptions, ProbeTarget
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import problems

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
NO_PROBE = "没有提供 {probe} 探针"


@dataclass(frozen=True)
class Screenshot:
    path: str
    note: str


def _route(endpoint: str) -> str:
    return endpoint.split(" ", 1)[-1]


Existing = Callable[[Signal], str | None]


def _none(signal: Signal) -> str | None:
    return None


def existing_problems(conn: sqlite3.Connection, layout: WorkspaceLayout, own: Collection[str]) -> Existing:
    """修复前已存在的问题：数据库中未解决、未并入他处且不属于本 Issue 的问题，按指纹(含别名)比对。信号先按
    normalize.yaml 规范化，与聚合时的指纹一致；算不出指纹的信号视为新出现。"""
    known: dict[str, str] = {}
    for problem in problems.find(conn):
        if problem.status is ProblemStatus.RESOLVED or problem.merged_into is not None or problem.id in own:
            continue
        for value in (problem.fingerprint, *problems.aliases(conn, problem.id)):
            known[value] = problem.id
    rules = normalize_rules.load(layout.normalize_rules())

    def existing(signal: Signal) -> str | None:
        try:
            value = fingerprint(replace(signal, normalized_message=normalize(signal.message, rules)), CURRENT_VERSION)
        except ValueError:
            return None
        return known.get(value) if value is not None else None

    return existing


def _split(signals: Sequence[Signal], existing: Existing) -> tuple[list[Signal], str | None]:
    """把信号分为本次新出现的与修复前已存在的；后者写成一句说明。"""
    found = [(signal, existing(signal)) for signal in signals]
    known = sorted({problem_id for _, problem_id in found if problem_id is not None})
    note = f"修复前已存在的问题 {'、'.join(known)} 仍出现，不计失败" if known else None
    return [signal for signal, problem_id in found if problem_id is None], note


def shallow(probe: Any | None, target: ProbeTarget, endpoints: Sequence[str],
            existing: Existing = _none) -> Item | None:
    if not endpoints:
        return None
    command = "api-fuzz shallow --include-path " + " ".join(_route(item) for item in endpoints)
    if probe is None:
        return Item("api-shallow", API_SHALLOW, CheckResult.UNVERIFIED, command,
                    reason=NO_PROBE.format(probe="api-fuzz"))
    routes = tuple(_route(item) for item in endpoints)
    outcome = probe.run(target, ProbeLevel.SHALLOW, ProbeOptions(include_paths=routes))
    if outcome.status is RunStatus.FAILED:
        return Item("api-shallow", API_SHALLOW, CheckResult.UNVERIFIED, command, reason="；".join(outcome.notes))
    introduced, note = _split(outcome.signals, existing)
    if introduced:
        found = "；".join(f"{signal.location} {signal.check}" for signal in introduced)
        reason = "；".join(part for part in (f"浅跑出现新的失败：{found}", note) if part)
        return Item("api-shallow", API_SHALLOW, CheckResult.FAIL, command, outcome.artifacts, reason)
    return Item("api-shallow", API_SHALLOW, CheckResult.PASS, command, outcome.artifacts, note)


def patrol(runner: Any | None, target: ProbeTarget, pages: Sequence[str]) -> tuple[Item | None, list[Screenshot]]:
    if not pages:
        return None, []
    command = "page patrol"
    if runner is None:
        return Item("page-patrol", PAGE_PATROL, CheckResult.UNVERIFIED, command,
                    reason=NO_PROBE.format(probe="页面运行器")), []
    outcome = runner.run(target)
    if outcome.status in (RunStatus.FAILED, RunStatus.SKIPPED):
        return Item("page-patrol", PAGE_PATROL, CheckResult.UNVERIFIED, command, reason="；".join(outcome.notes)), []
    shots = [Screenshot(path, f"受影响的页面 {'、'.join(pages)} 的巡检截图") for path in outcome.artifacts
             if path.lower().endswith(IMAGE_SUFFIXES)]
    wanted = set(pages)
    failures = [item for item in outcome.failures
                if item.page in wanted or normalize_location(item.page) in wanted]
    if failures:
        found = "；".join(f"{item.page} {item.message}" for item in failures)
        return (Item("page-patrol", PAGE_PATROL, CheckResult.FAIL, command, outcome.artifacts,
                     f"受影响页面上有失败：{found}"), shots)
    return Item("page-patrol", PAGE_PATROL, CheckResult.PASS, command, outcome.artifacts), shots
