"""修复的进度文档(redesign/05-fix.md 第 0 步)：第 0 步按通道写出检查清单，之后每一步更新它。

状态存在 data/fixes/<编号>/progress.json(检查清单、已完成、卡点、历史)，每次更新后重新渲染 progress.md
(progress 类型的交接文档)。通道升级时按新通道重排清单，已完成的步骤保留。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock, format_iso, parse_iso
from tightrein.domain.enums import DocumentStatus, Lane
from tightrein.domain.handoff.document import Event, NextStep
from tightrein.pipeline.fix.render import documents
from tightrein.store.files import atomic

FILE = "progress.json"
STEPS = {0: "分流", 1: "准备", 2: "勘察", 3: "出计划", 4: "确认计划", 5: "写复现测试", 6: "写代码", 7: "收集结果",
         8: "评审", 9: "完成"}
PENDING, DONE, BLOCKED, FAILED, SKIPPED = "pending", "done", "blocked", "failed", "skipped"
SCHEMA_STATES = {SKIPPED: DONE}  # 数据块只有 pending、done、blocked、failed；跳过的步骤记为完成并在文字中写明
LANE_STEPS = {Lane.FAST: (0, 1, 5, 6, 7, 8, 9), Lane.STANDARD: tuple(range(10)), Lane.LARGE: (0, 1, 3, 4)}


def steps_for(lane: Lane | None, *, scout: bool, repro: bool) -> tuple[int, ...]:
    """通道经过的步骤；B 通道不需要勘察、类型不写复现测试时去掉对应的步骤；超限只有分流。"""
    if lane is None:
        return (0,)
    return tuple(step for step in LANE_STEPS[lane] if (step != 2 or scout) and (step != 5 or repro))


@dataclass
class Tracker:
    directory: Path
    issue_id: str
    clock: Clock
    language: str
    zone: tzinfo | None = None

    @property
    def path(self) -> Path:
        return self.directory / FILE

    def _load(self) -> dict[str, Any]:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"lane": None, "steps": {}, "history": [], "blockers": []}

    def start(self, lane: Lane | None, steps: Sequence[int], text: str) -> None:
        """写出(或按新通道重排)检查清单；已有的步骤状态保留。"""
        data = self._load()
        known = data["steps"]
        data["lane"] = None if lane is None else lane.value
        data["steps"] = {str(step): known.get(str(step), {"state": PENDING, "note": ""}) for step in steps}
        self._save(data, text)

    def mark(self, step: int, state: str, text: str, *, blocker: str | None = None) -> None:
        data = self._load()
        data["steps"].setdefault(str(step), {"state": PENDING, "note": ""})
        data["steps"][str(step)] = {"state": state, "note": text}
        data["blockers"] = [blocker] if blocker else []
        self._save(data, f"第 {step} 步 {STEPS[step]}：{text}")

    def state(self, step: int) -> str | None:
        found = self._load()["steps"].get(str(step))
        return None if found is None else found["state"]

    def _save(self, data: dict[str, Any], event: str) -> None:
        now = self.clock.now()
        data["history"].append({"at": format_iso(now), "text": event})
        atomic.write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        self._render(data, now)

    def _render(self, data: dict[str, Any], now: datetime) -> None:
        steps = sorted(data["steps"].items(), key=lambda pair: int(pair[0]))
        checklist = [{"item": f"第 {step} 步 {STEPS[int(step)]}" + (f"：{item['note']}" if item["note"] else ""),
                      "state": SCHEMA_STATES.get(item["state"], item["state"]),
                      "owner": "user" if item["state"] == BLOCKED else "system"} for step, item in steps]
        completed = [f"第 {step} 步 {STEPS[int(step)]}" + ("(跳过)" if item["state"] == SKIPPED else "")
                     for step, item in steps if item["state"] in (DONE, SKIPPED)]
        remaining = [f"第 {step} 步 {STEPS[int(step)]}" for step, item in steps if item["state"] == PENDING]
        states = {item["state"] for _, item in steps}
        if FAILED in states:
            status = DocumentStatus.FAILED
        elif BLOCKED in states:
            status = DocumentStatus.BLOCKED
        elif PENDING not in states:
            status = DocumentStatus.DONE
        else:
            status = DocumentStatus.IN_PROGRESS
        lane = Lane(data["lane"]).label if data["lane"] else "超限"
        conclusion = f"通道 {lane}；已完成 {len(completed)}/{len(steps)} 步" + (
            f"；卡在：{data['blockers'][0]}" if data["blockers"] else "") + "。"
        writer = documents.Writer(self.directory, self.issue_id, self.language, self.zone)
        writer.write(documents.PROGRESS, documents.progress(
            writer, now, status=status, conclusion=conclusion, checklist=checklist, completed=completed,
            blockers=data["blockers"], next_steps=[NextStep(item, "fix") for item in remaining[:1]],
            history=[Event(parse_iso(item["at"]), item["text"]) for item in data["history"]]))
