"""每日汇总(redesign/09-loop.md 第 4、7 节)：运行摘要与收件箱合为一份 progress 类型的交接文档
data/reports/daily-<日期>.md，每次运行结束时按当天的数据重写(data/reports/daily-<日期>.json 记当天各次运行与历史)。

- 结论：待处理几项、今天运行几次、失败几次；
- 检查清单：收件箱各项(需要用户，blocked)，没有时一项「没有待处理的事项」；之后是「接入中的项目」；
- 已完成：今天各次运行的产出(新发现、回归、已解决、分诊、Issue、PR、自动决定)；
- 卡点：暂停、预算到达、未同步到 GitHub、网络换路与最近一次运行的异常(熔断的 Issue 在收件箱中)；
- 需要决定：每件的推荐做法；下一步：每件的命令；引用：今天各次运行的 JSON 交接文档；历史：每次运行一行。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import tzinfo
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock, format_iso, local_date, parse_iso
from tightrein.domain.enums import DocumentStatus
from tightrein.domain.handoff.document import Decision, Event, HandoffDocument, Header, NextStep, Reference
from tightrein.store.files import atomic, documents
from tightrein.store.files.layout import WorkspaceLayout

SOURCE = "loop"
NOTHING = "没有待处理的事项"
ONBOARDING_TITLE = "**接入中的项目**"
DECISION_LABELS = {"auto-approved": "自动放行", "needs-decision": "需要用户决定", "auto-merged": "自动合并",
                   "merge-waiting": "未自动合并"}


def _lines(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "无"


def _produced(run_id: str, made: Mapping[str, Any]) -> list[str]:
    parts = [f"{label} {'、'.join(made.get(key) or [])}" for key, label in
             (("newProblems", "新发现"), ("regressed", "回归"), ("resolved", "已解决"), ("issues", "Issue"))
             if made.get(key)]
    parts += [f"分诊 {len(made['triage'])} 个问题"] if made.get("triage") else []
    parts += [f"PR {url}" for url in made.get("pulls") or []]
    parts += [f"{item['subjectId']} {DECISION_LABELS[item['decision']]}" + (
        f"({'；'.join(item['reasons'])})" if item["reasons"] else "") for item in made.get("autonomy") or []]
    return [f"{run_id}：{'；'.join(parts)}"] if parts else []


def write(layout: WorkspaceLayout, clock: Clock, zone: tzinfo | None, language: str, run_id: str,
          values: Mapping[str, Any], onboarding: Sequence[str] = (), handoff: Path | None = None) -> Path:
    """写入(重写)当天的汇总，返回路径。values 为 loop 交接文档的 outputs。"""
    now = clock.now()
    day = local_date(now, zone)
    state_path = layout.daily_state(day)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"runs": []}
    failed = any(step.get("status") == "failed" for step in values["steps"])
    state["runs"] = [item for item in state["runs"] if item["runId"] != run_id] + [{
        "runId": run_id, "at": format_iso(now), "conclusion": values["conclusion"], "failed": failed,
        "produced": _produced(run_id, values.get("produced") or {}),
        "handoff": layout.relative(handoff) if handoff is not None else None}]
    atomic.write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    inbox = list(values["waiting"])
    checklist = [{"item": f"[{item['kind']}] {item['subjectId']} {item['summary']}", "state": "blocked",
                  "owner": "user"} for item in inbox] or [{"item": NOTHING, "state": "done", "owner": "system"}]
    listing = [f"[ ] [{item['kind']}] {item['subjectId']} {item['summary']}：`{item['command']}`" for item in inbox]
    checklist_text = _lines(listing or [NOTHING]) + f"\n\n{ONBOARDING_TITLE}\n\n" + _lines(list(onboarding))
    blockers = [*(f"已停下：{text}" for text in (values.get("halted") or {}).values()),
                *(f"未同步到 GitHub：{line}" for line in values.get("unsynced") or []),
                *(f"网络换路：{line}" for line in values.get("reroutes") or []),
                *(f"{item['source']}：{item['reason']}" for item in values["anomalies"])]
    completed = [line for item in state["runs"] for line in item["produced"]]
    runs_failed = sum(1 for item in state["runs"] if item["failed"])
    conclusion = (f"待处理 {len(inbox)} 项" if inbox else NOTHING) + \
        f"；今天运行 {len(state['runs'])} 次" + (f"，{runs_failed} 次有步骤失败" if runs_failed else "") + "。"
    header = Header("progress", f"daily-{day.isoformat()}", DocumentStatus.BLOCKED if inbox else DocumentStatus.DONE,
                    SOURCE, "user", day.isoformat(), parse_iso(state["runs"][0]["at"]), now)
    document = HandoffDocument(
        header, conclusion,
        {"checklist": checklist_text, "completed": _lines(completed or ["今天还没有新的产出"]),
         "blockers": _lines(blockers)},
        {"checklist": checklist},
        decisions=tuple(Decision(f"{item['subjectId']} {item['summary']}", item.get("recommendation") or "按命令处理",
                                 item.get("reason") or "需要用户处理") for item in inbox),
        next_steps=tuple(NextStep(item["command"], "user") for item in inbox),
        references=tuple(Reference(item["handoff"], f"运行 {item['runId']} 的交接文档") for item in state["runs"]
                         if item["handoff"]),
        history=tuple(Event(parse_iso(item["at"]), f"运行 {item['runId']}：{item['conclusion']}")
                      for item in state["runs"]))
    path = layout.daily_report(day)
    documents.write(path, document, language, zone)
    return path
