"""第 4 步 探针调用(architecture/05 2.5)：按选择器与命令行参数组装 ProbeOptions，调用探针。

各探针自己读取 project.yaml 的 probes 段，这里只放命令行与运行上下文给出的参数：选择器、重新解析、迁移归档、
static 的审查器与增量起点(上一次成功的 static 运行的目标 commit)、配套 server-log 的主运行编号；
配套运行的时间窗在 ProbeTarget 上。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import ProbeLevel, RunStage, RunStatus
from tightrein.sources.base import Probe, ProbeOptions, ProbeOutcome, ProbeTarget
from tightrein.sources.static.reviewer import Reviewer
from tightrein.store.repos import pending_claims, runs
from tightrein.store.repos.pending_claims import PendingClaim

SELECTORS: dict[str, tuple[str, frozenset[ProbeKind]]] = {
    "role": ("roles", frozenset({ProbeKind.API_FUZZ})),
    "path": ("include_paths", frozenset({ProbeKind.API_FUZZ})),
    "name": ("names", frozenset({ProbeKind.PROJECT_PROBE})),
    "pending": ("pending", frozenset({ProbeKind.STATIC})),
}
REPEATABLE = frozenset({"roles", "include_paths", "names", "pending"})


def parse_selectors(probe: ProbeKind, values: Sequence[str]) -> dict[str, Any]:
    """`role:<角色>`、`path:<路径>`(api-fuzz)、`name:<探针名>`(project-probe)、`pending:<疑点编号>|low`(static，
    取证待处理清单中的疑点)，都可以重复。方法不支持的写法抛出 ValueError。"""
    options: dict[str, Any] = {}
    for value in values:
        kind, _, argument = value.partition(":")
        if kind not in SELECTORS or not argument:
            raise ValueError(f"无法识别的选择器：{value}，只能是 role:<角色>、path:<路径>、name:<探针名>、pending:<编号>")
        name, probes = SELECTORS[kind]
        if probe not in probes:
            raise ValueError(f"{probe.value} 不支持选择器 {kind}:")
        if name in REPEATABLE:
            options[name] = (*options.get(name, ()), argument)
        elif name in options:
            raise ValueError(f"选择器 {kind}: 只能给一次")
        else:
            options[name] = argument
    return options


def base_commit(conn: sqlite3.Connection) -> str | None:
    """上一次成功的 static 运行的目标 commit，增量审查从它之后开始。"""
    previous = [run for run in runs.find(conn, stage=RunStage.COLLECT, probe=ProbeKind.STATIC, status=RunStatus.OK)
                if run.target_commit is not None]
    return previous[-1].target_commit if previous else None


def queued_claims(conn: sqlite3.Connection, selected: Sequence[str]) -> tuple[PendingClaim, ...]:
    """静态巡检要取证的待处理疑点：没有选择器时为超出上限遗留的疑点；`pending:low` 为全部低级疑点，
    `pending:<编号>` 为指定的疑点(不存在或已取证时抛出 ValueError)。"""
    if not selected:
        return tuple(pending_claims.find(conn, reason=pending_claims.OVER_LIMIT))
    chosen: dict[str, PendingClaim] = {}
    for value in selected:
        if value == pending_claims.LOW:
            chosen.update({item.id: item for item in pending_claims.find(conn, reason=pending_claims.LOW)})
            continue
        found = pending_claims.get(conn, value)
        if found is None or found.state != pending_claims.PENDING:
            raise ValueError(f"待处理清单中没有未取证的疑点 {value}")
        chosen[found.id] = found
    return tuple(chosen.values())


def options_for(conn: sqlite3.Connection, probe: ProbeKind, *, selectors: Sequence[str] = (), reparse: bool = False,
                import_archive: Path | None = None, reviewer: Reviewer | None = None) -> ProbeOptions:
    values = parse_selectors(probe, selectors)
    if probe is ProbeKind.STATIC:
        values.update(reviewer=reviewer, base_commit=base_commit(conn),
                      queued=queued_claims(conn, values.get("pending", ())))
    if probe is ProbeKind.INCIDENTAL:
        values["import_archive"] = import_archive
    return ProbeOptions(from_raw=reparse, **values)


def execute(probe: Probe, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome:
    return probe.run(target, level, options)
