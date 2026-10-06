"""运行摘要与本机通知(architecture/09 3.7，redesign/09-loop.md 第 4、7 节)。

交接文档 loop-<运行编号>(handoff/outputs/loop.schema.json)由本次各步骤的结果与子运行的交接文档汇总，给程序读取；
给人读的是当天的每日汇总(digest.py，progress 类型)，运行摘要与收件箱合为一份。「等待用户」即收件箱(inbox.py)。
issues.tracker 为 github 时另列「未同步到 GitHub」；本次 git、gh 遇到网络类错误换路重试过时列「网络换路」；本次有
自动决定(gates.issue-approve 的放行或需要用户决定、gates.merge 的合并与未合并原因)时列「自动决定」；暂停或预算到达时
列 halted。每次 run 结束发一条通知：标题为「tightrein：<工作区>」，正文为结论、待处理事项数、健康检查的立即通知项
与汇总路径；没有新产出且没有等待事项的定时运行不发。通知失败写入「异常」，不影响运行。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import tzinfo
from pathlib import Path
from typing import Any

from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.config.project import ProjectConfig
from tightrein.contracts import versions
from tightrein.domain.clock import Clock, format_iso, local_date, parse_iso
from tightrein.domain.enums import ProblemStatus, RunStage, RunStatus
from tightrein.observability.notify import FAILED, Notifier, NotifyResult
from tightrein.orchestrator import digest, inbox
from tightrein.orchestrator.runlog import LoopRun
from tightrein.pipeline.issue.steps import github
from tightrein.store.files import handoff_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, runs

ENVELOPE = "handoff/envelope.schema.json"
NOTIFY_EVENT = "loop-run"
TITLE = "tightrein：{workspace}"
PRODUCED_KEYS = ("newProblems", "regressed", "resolved", "triage", "issues", "pulls", "autonomy")
STATUS_KEYS = {ProblemStatus.NEW.value: "newProblems", ProblemStatus.REGRESSED.value: "regressed",
               ProblemStatus.RESOLVED.value: "resolved"}


def _decision(subject: str, decision: str, reasons: Sequence[str]) -> dict[str, Any]:
    return {"subjectId": subject, "decision": decision, "reasons": list(reasons)}


def produced(conn: sqlite3.Connection, layout: WorkspaceLayout, run_ids: Sequence[str]) -> dict[str, list[Any]]:
    """autonomy 为本次的自动决定：issue 交接文档中的放行决定，release 交接文档中本次运行期间做出的合并判断(交接文档
    累积，判断时间早于本次第一个子运行的不计)。"""
    found: dict[str, list[Any]] = {key: [] for key in PRODUCED_KEYS}
    started = [run.started_at for run in (runs.get(conn, run_id) for run_id in run_ids) if run is not None]
    merges: set[str] = set()
    for run_id in run_ids:
        for record in handoffs.for_run(conn, run_id):
            outputs = handoff_files.read(layout.root / record.path)["outputs"]
            if record.stage is RunStage.AGGREGATE and "statusChanges" in outputs:
                for change in outputs["statusChanges"]:
                    key = STATUS_KEYS.get(change["to"])
                    if key is not None and change["problemId"] not in found[key]:
                        found[key].append(change["problemId"])
            elif record.stage is RunStage.TRIAGE and "verdict" in outputs:
                found["triage"].append({"problemId": outputs["problemId"], "title": outputs["claim"]["title"],
                                        "verdict": outputs["verdict"], "severity": outputs.get("severity"),
                                        "disposition": outputs["disposition"]})
            elif record.stage is RunStage.ISSUE and outputs.get("issueId"):
                if outputs["issueId"] not in found["issues"]:
                    found["issues"].append(outputs["issueId"])
                if outputs.get("autonomy"):
                    decided = outputs["autonomy"]
                    found["autonomy"].append(_decision(outputs["issueId"], "auto-approved" if decided["approved"]
                                                       else "needs-decision", decided["reasons"]))
            elif record.stage is RunStage.RELEASE:
                if outputs.get("pr") and outputs["pr"]["url"] not in found["pulls"]:
                    found["pulls"].append(outputs["pr"]["url"])
                merge = outputs.get("autoMerge")
                if merge and started and parse_iso(merge["at"]) >= min(started) and outputs["issueId"] not in merges:
                    merges.add(outputs["issueId"])
                    found["autonomy"].append(_decision(outputs["issueId"], "auto-merged" if merge["merged"]
                                                       else "merge-waiting", merge["reasons"]))
    return found


def conclusion(waiting_items: Sequence[Mapping[str, Any]], failed: Sequence[str]) -> str:
    parts = []
    if waiting_items:
        parts.append(f"有 {len(waiting_items)} 项等待用户处理")
    if failed:
        parts.append(f"{len(failed)} 个步骤失败：{'、'.join(failed)}")
    return "；".join(parts) if parts else "没有需要处理的事项，也没有失败"


def outputs(conn: sqlite3.Connection, layout: WorkspaceLayout, config: ProjectConfig, steps: Sequence[Any],
            anomalies: Sequence[Mapping[str, Any]], run_ids: Sequence[str],
            reroutes: Sequence[str] = (), halted: Mapping[str, str] | None = None) -> dict[str, Any]:
    items = inbox.items(conn, layout, unattended=gates.auto(config, Gate.FIX_SESSION))
    failed = [step.name for step in steps if step.status is RunStatus.FAILED]
    return {"conclusion": conclusion(items, failed), "waiting": items, "steps": [step.to_dict() for step in steps],
            "produced": produced(conn, layout, run_ids), "anomalies": [dict(item) for item in anomalies],
            "unsynced": github.unsynced(conn, config), "reroutes": list(reroutes), "halted": dict(halted or {})}


def has_news(values: Mapping[str, Any]) -> bool:
    return bool(values["waiting"]) or any(values["produced"].get(key) for key in PRODUCED_KEYS)


def write(layout: WorkspaceLayout, conn: sqlite3.Connection, clock: Clock, loop: LoopRun, values: Mapping[str, Any],
          *, language: str, zone: tzinfo | None = None, onboarding: Sequence[str] = (),
          daily: bool = True) -> tuple[Path, Path]:
    """写 loop 交接文档(JSON)并重写当天的每日汇总，返回两者的路径；daily 为假(没有执行任何事件步骤的事件运行)时
    不重写汇总。"""
    blocked = values["waiting"] or any(step["status"] == "failed" for step in values["steps"])
    document: dict[str, Any] = {
        "schemaVersion": versions.current(ENVELOPE), "runId": loop.id, "stage": RunStage.LOOP.value,
        "subject": {"type": "run", "id": loop.id}, "status": "ok", "inputsRef": {}, "outputs": dict(values),
        "nextAction": "处理「等待用户」中的事项" if blocked else "无需处理", "createdAt": format_iso(clock.now()),
    }
    handoff = handoff_files.write(layout, document, clock, conn=conn).path
    report = digest.write(layout, clock, zone, language, loop.id, values, onboarding, handoff) if daily else \
        layout.daily_report(local_date(clock.now(), zone))
    return handoff, report


def notify(notifier: Notifier | None, workspace: str, run_id: str, values: Mapping[str, Any], report: Path,
           health: Sequence[Mapping[str, Any]], *, scheduled: bool) -> NotifyResult | None:
    if notifier is None or (scheduled and not has_news(values) and not health):
        return None
    lines = [values["conclusion"], f"待处理 {len(values['waiting'])} 项"]
    lines += [f"健康检查：{item['detail']}" for item in health]
    lines.append(f"每日汇总：{report}")
    return notifier.notify(NOTIFY_EVENT, run_id, "\n".join(lines), title=TITLE.format(workspace=workspace))


def failed_notice(result: NotifyResult | None) -> dict[str, Any] | None:
    if result is None or result.status != FAILED:
        return None
    return {"source": "notify", "reason": result.reason or "通知失败", "log": None}
