"""运行摘要中分诊一节(architecture/06 3.8、7.4)：P0 提 Issue 置顶，人工队列单列，并入与跳过的问题单列。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tightrein.domain.enums import Disposition, HandoffStatus, Severity, Verdict


@dataclass(frozen=True)
class SummaryItem:
    problem_id: str
    title: str
    status: HandoffStatus
    verdict: Verdict | None = None
    severity: Severity | None = None
    disposition: Disposition | None = None
    merged_into: str | None = None
    reason: str | None = None


def _line(item: SummaryItem) -> str:
    parts = [item.verdict.label if item.verdict else "", item.severity.value if item.severity else "",
             item.disposition.label if item.disposition else ""]
    return f"- {item.problem_id} {item.title}：{'，'.join(part for part in parts if part)}"


def summary(run_id: str, items: Sequence[SummaryItem], skipped: Sequence[tuple[str, str]] = (),
            notes: Sequence[str] = ()) -> str:
    urgent = [item for item in items if item.severity is Severity.P0 and item.disposition is Disposition.CREATE_ISSUE]
    manual = [item for item in items if item.disposition is Disposition.MANUAL_QUEUE]
    merged = [item for item in items if item.merged_into is not None]
    failed = [item for item in items if item.status is HandoffStatus.FAILED]
    others = [item for item in items if item not in urgent and item not in manual and item not in merged
              and item not in failed]
    lines = [f"## 分诊 {run_id}", ""]
    if urgent:
        lines += ["P0 问题，已交给 issue 创建：", *(_line(item) for item in urgent), ""]
    lines += [*(_line(item) for item in others)] or ["本次没有得出结论的问题。"]
    if manual:
        lines += ["", "人工队列(补充信息后 tightrein problem retriage <问题> --note，或直接改判)：",
                  *(f"{_line(item)}；{item.reason}" for item in manual)]
    if merged:
        lines += ["", "并入其他问题：", *(f"- {item.problem_id} 并入 {item.merged_into}" for item in merged)]
    if failed:
        lines += ["", "出错：", *(f"- {item.problem_id} {item.reason}" for item in failed)]
    if skipped:
        lines += ["", "未处理：", *(f"- {problem_id}：{reason}" for problem_id, reason in skipped)]
    if notes:
        lines += ["", "说明：", *(f"- {note}" for note in notes)]
    return "\n".join(lines) + "\n"
