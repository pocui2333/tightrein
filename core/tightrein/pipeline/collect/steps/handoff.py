"""第 7 步 交接(architecture/05 2.5、2.7)：组装 outputs，写 signals.ndjson、交接文档与运行摘要。

signalsFile 与 rawDir 相对运行目录(交接文档所在 handoff/ 的上一级)，正常模式与 --output 模式写法相同。
--output 模式不写 handoffs 表与 data/reports/ 下的运行摘要。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.contracts import versions
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import HandoffStatus, RunStage, RunStatus
from tightrein.domain.run import Run
from tightrein.domain.signal import Signal
from tightrein.pipeline.collect.render.summary import summary
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome
from tightrein.pipeline.collect.steps.target import TargetInfo
from tightrein.sources.base import ProbeOutcome
from tightrein.store.files import atomic, handoff_files
from tightrein.store.files.layout import WorkspaceLayout

ENVELOPE = "handoff/envelope.schema.json"
SIGNALS_FILE = "signals.ndjson"
TO_AGGREGATE = "交给 aggregate"
HANDOFF_STATUS = {
    RunStatus.OK: HandoffStatus.OK,
    RunStatus.PARTIAL: HandoffStatus.OK,
    RunStatus.SKIPPED: HandoffStatus.OK,
    RunStatus.BLOCKED: HandoffStatus.BLOCKED,
    RunStatus.FAILED: HandoffStatus.FAILED,
}


def coverage_counts(run: Run) -> dict[str, Any]:
    coverage = run.coverage
    return {
        "endpoints": len(coverage.tested_endpoints()), "endpointsTotal": coverage.endpoints_total,
        "files": len(coverage.files), "methods": coverage.methods, "sources": list(coverage.sources),
    }


def _stats(outcome: ProbeOutcome | None) -> dict[str, Any]:
    if outcome is None:
        return {}
    stats: dict[str, Any] = {key: value for key, value in outcome.stats.items() if value is not None}
    if outcome.extensions:
        stats["extensions"] = {point: dict(entry) for point, entry in outcome.extensions.items()}
    return stats


def outputs(run: Run, info: TargetInfo, outcome: ProbeOutcome | None, signals: Sequence[Signal],
            regression_outcomes: Sequence[RegressionOutcome], *, notes: Sequence[str],
            disabled: Mapping[str, str] | None = None) -> dict[str, Any]:
    if run.probe is None:
        raise ValueError(f"{run.id} 不是 collect 运行")
    return {
        "probe": run.probe.value,
        "level": run.level.value if run.level is not None else None,
        "target": {"environment": info.environment, "baseUrl": info.base_url, "release": info.release,
                   "worktreeHead": info.worktree_head},
        "runStatus": run.status.value,
        "environment": run.environment_detail.to_dict(),
        "coverage": coverage_counts(run),
        "signalsFile": SIGNALS_FILE,
        "signalCount": len(signals),
        "signalsByCheck": dict(sorted(Counter(signal.check for signal in signals).items())),
        "stats": _stats(outcome),
        "regressions": [{"issue": item.check.issue_id, "checkId": item.check.check_id, "result": item.result.value}
                        for item in regression_outcomes],
        "rawDir": f"raw/{run.probe.value}",
        "skippedReason": outcome.skipped_reason if outcome is not None else None,
        "notes": list(notes),
        **({"disabledSources": dict(disabled)} if disabled else {}),
    }


def next_action(status: HandoffStatus, reason: str | None, hint: str | None) -> str:
    if status is HandoffStatus.OK:
        return TO_AGGREGATE
    return "；".join(part for part in (reason, hint) if part) or "查看运行摘要中的原因后重新运行"


def document(run: Run, outputs_: Mapping[str, Any], clock: Clock, *, reason: str | None,
             hint: str | None) -> dict[str, Any]:
    status = HANDOFF_STATUS[run.status]
    result: dict[str, Any] = {
        "schemaVersion": versions.current(ENVELOPE),
        "runId": run.id,
        "stage": RunStage.COLLECT.value,
        "subject": {"type": "run", "id": run.id},
        "status": status.value,
        "inputsRef": {"commits": [run.target_commit]} if run.target_commit else {},
        "outputs": dict(outputs_),
        "nextAction": next_action(status, reason, hint),
        "createdAt": format_iso(clock.now()),
    }
    if status is not HandoffStatus.OK:
        result["blockedReason"] = reason or "; ".join(outputs_["notes"]) or run.status.label
    return result


def write(layout: WorkspaceLayout, conn: sqlite3.Connection | None, clock: Clock, run: Run,
          outputs_: Mapping[str, Any], signals: Sequence[Signal], *, reason: str | None = None,
          hint: str | None = None) -> Path:
    """conn 为空(--output 模式)时不写 handoffs 表与运行摘要。返回交接文档的路径。"""
    lines = "".join(json.dumps(signal.to_dict(), ensure_ascii=False) + "\n" for signal in signals)
    atomic.write_text(layout.signals_file(run.id), lines)
    written = handoff_files.write(layout, document(run, outputs_, clock, reason=reason, hint=hint), clock, conn=conn)
    if conn is not None:
        atomic.write_text(layout.run_report(run.id), summary(run.id, outputs_))
    return written.path


def read_signals(handoff: Path, outputs_: Mapping[str, Any]) -> list[Signal]:
    """按交接文档的 signalsFile 读取信号(aggregate --input 使用)。"""
    path: Path = handoff.parent.parent / outputs_["signalsFile"]
    return [Signal.from_dict(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]
