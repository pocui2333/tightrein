"""定位(implement.locate)：补全代码笔记中缺的部分。

- 评估已留下代码笔记、核心位置在修复 worktree 中仍然成立时跳过，不调用模型；
- 否则只读调用一次(把已有笔记交给它，只补缺的)，每个位置由程序核对真实存在，不合格带原因重做；
- 通过后由程序截取原文与签名补进 `00-issue-notes.json`，方案、编码、审查都读它，不再各自通读代码。
重出方案时不再回到这一步(implement.py 直接回到方案)，上次通过的定位结论原样复用。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.assess import notes as notes_file
from tightrein.assess.notes import CodeNotes
from tightrein.implement.locate import brief
from tightrein.implement.prompts import locate as prompts
from tightrein.implement.prompts.common import Ask, Usage, ask, failure_text, worktree_of
from tightrein.protocol.handoff import Handoff, Status

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime

POINT = "implement.locate"
SCHEMA = Path(__file__).with_name("locate.schema.json")
SKIPPED = "评估留下的代码笔记有核心位置且仍成立"


def run(runtime: Runtime, context: ImplementContext) -> Handoff:
    worktree = worktree_of(context)
    if brief.sufficient(context.notes, worktree):
        known = context.notes.files if context.notes is not None else []
        return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=Status.PASSED,
                       summary=f"{SKIPPED}，跳过定位",
                       facts={"skipped": SKIPPED, "added": [], "files": list(known), "missing": []})
    usage = Usage()
    stale = brief.missing_core(context.notes, worktree)
    feedback = [f"笔记中的核心位置在当前代码中已不成立，重新确认：{location}" for location in stale]
    rounds = int(runtime.settings.control(POINT, "rounds"))
    problems: list[str] = []
    for _ in range(rounds + 1):
        result = ask(runtime, context, Ask(POINT, prompts.variables(runtime, context, feedback), SCHEMA,
                                           conditions=prompts.conditions(runtime, context)), usage)
        if not result.ok or result.output is None:
            problems.append(failure_text(result))
            continue
        output = brief.complete(result.output, worktree)
        found = brief.check(output, worktree)
        if found:
            feedback = found
            problems += found
            continue
        return _passed(runtime, context, output, worktree, usage)
    return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=Status.FAILED,
                   summary=f"定位重做 {rounds} 次仍不合格", facts={"skipped": None, "problems": problems},
                   metrics=usage.metrics(rounds=rounds + 1), versions=usage.versions(runtime))


def _passed(runtime: Runtime, context: ImplementContext, output: dict[str, Any], worktree: Path,
            usage: Usage) -> Handoff:
    found = brief.findings(output)
    notes = context.notes or CodeNotes(subject=context.issue.id, commit=context.base_commit or "")
    notes.add(found, worktree)
    notes.trigger = notes.trigger or output.get("trigger")
    notes_file.save(runtime.workspace, notes)
    facts = {"skipped": None, "added": found, "files": list(notes.files), "trigger": output.get("trigger"),
             "missing": output.get("missing") or [], "affectedEndpoints": output.get("affectedEndpoints") or [],
             "affectedPages": output.get("affectedPages") or [],
             "incidentalFindings": output.get("incidentalFindings") or [],
             "baseCommit": context.base_commit,
             "knowledgeSuggestions": output.get("knowledgeSuggestions") or []}
    summary = f"补全代码笔记 {len(found)} 处，涉及 {len(brief.files(found))} 个文件"
    return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=Status.PASSED, summary=summary,
                   facts=facts, notes=output.get("analysis"),
                   metrics=usage.metrics(produced={"locations": len(found)}), versions=usage.versions(runtime))
