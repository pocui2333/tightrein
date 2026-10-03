"""采集方法的统一接口(architecture/04 1.3、1.4)：Probe 协议、ProbeTarget、ProbeOptions、ProbeOutcome。

- ProbeTarget 描述本次运行的目标：环境、地址、目标版本、只读 worktree、运行编号、本方法的原始输出目录与时钟。
- ProbeOptions 汇集各方法的参数，各方法只读取自己用到的字段；缺省值来自 project.yaml 的 sources 段，
  命令行参数覆盖配置，这一合并由调用方(collect)完成。
- ProbeOutcome 的 status 只取 ok、partial、failed、skipped；skipped 与 failed 须写明原因(skipped_reason 或 notes)。
  平台来源的读取位置、项目探针的状态、静态巡检的待处理疑点与 incidental 的已读记录随结果返回，由 collect 在写入
  信号的同一事务中经 save_state 保存。
- artifacts 与信号 context 中的 *Ref、reportPath 等路径都相对本方法的原始输出目录(target.raw_dir)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from tightrein.domain.clock import Clock
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import ProbeLevel, RunStatus
from tightrein.domain.run import Coverage, EnvironmentDetail
from tightrein.domain.signal import Signal
from tightrein.store.repos import incidental_sources, pending_claims, probe_states, source_cursors
from tightrein.store.repos.incidental_sources import IncidentalSource
from tightrein.store.repos.pending_claims import PendingClaim
from tightrein.store.repos.probe_states import ProbeState
from tightrein.store.repos.source_cursors import SourceCursor

if TYPE_CHECKING:
    from tightrein.sources.static.reviewer import Reviewer

ENVIRONMENTS = ("staging", "production", "local")
OUTCOME_STATUSES = (RunStatus.OK, RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.SKIPPED)
PROBE_LEVELS: dict[ProbeKind, tuple[ProbeLevel, ...]] = {
    ProbeKind.API_FUZZ: (ProbeLevel.SHALLOW, ProbeLevel.DEEP),
    ProbeKind.STATIC: (ProbeLevel.INCREMENTAL, ProbeLevel.FULL, ProbeLevel.BASELINE),
    ProbeKind.PLATFORM_ERRORS: (),
    ProbeKind.ACCESS_LOG: (),
    ProbeKind.ALERTS: (),
    ProbeKind.PROJECT_PROBE: (),
    ProbeKind.INCIDENTAL: (),
}


def resolve_level(kind: ProbeKind, level: ProbeLevel | None) -> ProbeLevel | None:
    """档位的缺省值为第一个可用档位；没有档位的探针忽略传入值，返回空；不可用的档位抛出 ValueError。"""
    levels = PROBE_LEVELS[kind]
    if not levels:
        return None
    if level is None:
        return levels[0]
    if level not in levels:
        allowed = "、".join(item.value for item in levels)
        raise ValueError(f"{kind.value} 的档位只能是 {allowed}：{level.value}")
    return level


@dataclass(frozen=True)
class ProbeTarget:
    environment: str
    run_id: str
    raw_dir: Path
    clock: Clock
    base_url: str | None = None
    release: str | None = None
    worktree: Path | None = None

    def __post_init__(self) -> None:
        if self.environment not in ENVIRONMENTS:
            raise ValueError(f"environment 只能是 {'、'.join(ENVIRONMENTS)}：{self.environment}")


@dataclass(frozen=True)
class ProbeOptions:
    """各采集方法的参数(architecture/04 1.4)；为空的字段取方法的缺省行为。"""

    roles: tuple[str, ...] = ()
    include_paths: tuple[str, ...] = ()
    exclude_path_regex: str | None = None
    max_examples: int | None = None
    phases: tuple[str, ...] = ()
    include_methods: tuple[str, ...] = ()
    max_response_time: float | None = None
    seed: int | None = None
    from_raw: bool = False
    base_commit: str | None = None
    reviewer: Reviewer | None = None
    max_claims: int | None = None
    pending: tuple[str, ...] = ()
    queued: tuple[PendingClaim, ...] = ()
    import_archive: Path | None = None
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeOutcome:
    status: RunStatus
    signals: tuple[Signal, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    environment: EnvironmentDetail = field(default_factory=EnvironmentDetail)
    stats: Mapping[str, float] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    extensions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    skipped_reason: str | None = None
    cursors: tuple[SourceCursor, ...] = ()
    sources: tuple[IncidentalSource, ...] = ()
    pending_claims: tuple[PendingClaim, ...] = ()
    probe_states: tuple[ProbeState, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in OUTCOME_STATUSES:
            raise ValueError(f"探针结果的状态只能是 ok、partial、failed、skipped：{self.status.value}")
        if self.status is RunStatus.SKIPPED and not self.skipped_reason:
            raise ValueError("skipped 的结果必须写明原因")
        if self.status is RunStatus.FAILED and not self.notes:
            raise ValueError("failed 的结果必须在 notes 中写明原因")


def skipped(reason: str, **changes: Any) -> ProbeOutcome:
    """notes 缺省为原因本身。"""
    changes.setdefault("notes", (reason,))
    return ProbeOutcome(RunStatus.SKIPPED, skipped_reason=reason, **changes)


def failed(*notes: str, **changes: Any) -> ProbeOutcome:
    return ProbeOutcome(RunStatus.FAILED, notes=tuple(notes), **changes)


def save_state(conn: sqlite3.Connection, outcome: ProbeOutcome) -> None:
    """保存随结果返回的读取位置、项目探针状态、待处理疑点与已读记录；调用方在写入信号的同一事务中调用。"""
    for cursor in outcome.cursors:
        source_cursors.save(conn, cursor)
    for state in outcome.probe_states:
        probe_states.save(conn, state)
    for claim in outcome.pending_claims:
        pending_claims.save(conn, claim)
    for source in outcome.sources:
        incidental_sources.save(conn, source)


class Probe(Protocol):
    name: ProbeKind
    levels: tuple[ProbeLevel, ...]

    def run(self, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome: ...
