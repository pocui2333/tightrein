"""用户在修复中的决定与补充说明(architecture/07 4.2)：data/fixes/<编号>/decisions.json。

记录 `fix plan --note`、`fix confirm --reject --note` 的原文与 `--accept-design` 的决定(来源、时间)，之后每次出计划与
实施都读取，经 FixContext.decisions 进入 fix-scout、fix-planner、frontend-designer、写复现测试与写代码的提示；
`--accept-design` 一经记录持续有效。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock, format_iso
from tightrein.store.files import atomic

FILE = "decisions.json"
PLAN_NOTE = "fix plan --note"
REJECT_NOTE = "fix confirm --reject --note"
ACCEPT_DESIGN = "fix plan --accept-design"
MANUAL_DESIGN = "用户需求"  # 用户亲自提出的需求：需求正文就是用户给出的修复方向，视为已同意按设计层面修复
DESIGN_TEXT = ("用户已同意按设计层面的根因修复。勘察中的设计问题照常写进 designIssue，但不要把它作为中止理由：照常给出"
               "完整的勘察结论与修复计划。")


@dataclass(frozen=True)
class Decisions:
    entries: tuple[dict[str, Any], ...] = ()
    design_accepted: bool = False

    def render(self) -> str:
        """「用户的决定与补充」一节的正文；没有记录时为空。"""
        lines = [f"- {item['at']}({item['source']})：{item['text']}" for item in self.entries]
        if not lines:
            return ""
        return "\n".join(["以下是用户在本 Issue 修复中给出的决定与补充，必须遵守并在输出中回应：", "", *lines])


def load(directory: Path) -> Decisions:
    path = directory / FILE
    if not path.is_file():
        return Decisions()
    data = json.loads(path.read_text(encoding="utf-8"))
    return Decisions(tuple(data.get("entries") or ()), bool(data.get("designAccepted")))


def _save(directory: Path, decisions: Decisions) -> None:
    atomic.write_text(directory / FILE, json.dumps({"designAccepted": decisions.design_accepted,
                                                    "entries": list(decisions.entries)},
                                                   ensure_ascii=False, indent=2) + "\n")


def record(directory: Path, clock: Clock, source: str, text: str) -> Decisions:
    current = load(directory)
    updated = Decisions((*current.entries, {"at": format_iso(clock.now()), "source": source, "text": text}),
                        current.design_accepted)
    _save(directory, updated)
    return updated


def accept_design(directory: Path, clock: Clock, source: str = ACCEPT_DESIGN) -> Decisions:
    current = load(directory)
    if current.design_accepted:
        return current
    _save(directory, Decisions(current.entries, True))
    return record(directory, clock, source, DESIGN_TEXT)
