"""运行摘要中的采集片段：采集方法、状态、目标、信号数、覆盖范围、复现检查、说明与未启用的方法。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tightrein.domain.enums import RunStatus


def summary(run_id: str, outputs: Mapping[str, Any]) -> str:
    status = RunStatus(outputs["runStatus"])
    target = outputs["target"]
    level = f"({outputs['level']})" if outputs["level"] else ""
    lines = [
        f"## 采集 {outputs['probe']}{level}",
        "",
        f"- 运行：{run_id}，{status.label}",
        f"- 目标：{target['baseUrl'] or '无'}，版本 {target['release'] or '未知'}",
        f"- 信号：{outputs['signalCount']} 条"
        + "".join(f"，{check} {count}" for check, count in outputs["signalsByCheck"].items()),
    ]
    coverage = outputs["coverage"]
    covered = [f"{name} {coverage[name]}" for name in ("endpoints", "files") if coverage[name]]
    if coverage["sources"]:
        covered.append(f"来源 {'、'.join(coverage['sources'])}")
    if covered:
        lines.append(f"- 覆盖：{'，'.join(covered)}")
    failed = [f"{item['issue']}/{item['checkId']} {item['result']}" for item in outputs["regressions"]
              if item["result"] != "passed"]
    if outputs["regressions"]:
        lines.append(f"- 复现检查：{len(outputs['regressions'])} 条" + (f"，未通过 {'、'.join(failed)}" if failed else ""))
    if outputs["skippedReason"]:
        lines.append(f"- 未运行的原因：{outputs['skippedReason']}")
    lines += [f"- {note}" for note in outputs["notes"]]
    disabled = outputs.get("disabledSources") or {}
    if disabled:
        lines.append("- 未启用的采集方法：" + "；".join(f"{name}({reason})" for name, reason in disabled.items()))
    return "\n".join(lines) + "\n"
