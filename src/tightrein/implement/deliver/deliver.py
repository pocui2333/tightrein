"""交付(implement.deliver)：把通过自检与审查的改动交给发布，写补丁与 Issue 历史，Issue 转为 releasing。

- 交付前再核对一次与提交无关的 diff 哈希：自检或审查之后工作区又有改动，就回到那一步(审查后变了只回到审查，不重新
  编码)，不把没审过的改动交出去；
- 必填事实(44c，按 deliver.schema.json 校验，键名照 release/record.parse_delivery)：分支、worktree、最后 commit、
  diff 哈希、改动文件、检查结果、审查结论、高风险路径，以及编码给出的提交与 PR 文字；发布只读这些，不再翻前面的步骤；
- 补丁文本把未跟踪的新文件以新增文件的形式附在后面，落盘为 `38-implement.deliver-diff.patch`；
- 弱证据与未验证的检查项照写进 unverified，不阻断(写成通过等同伪造)；
- rounds 汇总全部轮次：按各轮落盘的编码、自检、审查交接(`<序号>-<步骤>.r<轮>-handoff.json`)写每轮的结论与阻断项；
- Issue 状态经 assess/issue/transitions.apply_event 转换(DELIVER：implementing → releasing)，历史由它一并记下。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tightrein.assess.issue.transitions import IssueEvent, apply_event
from tightrein.implement.check.changes import Changes, collect
from tightrein.implement.context import ImplementContext
from tightrein.protocol import boundaries
from tightrein.protocol.handoff import Handoff, Metrics, Status, check_facts, load_schema, read
from tightrein.protocol.naming import FileName, format_iso
from tightrein.protocol.runtime import Runtime
from tightrein.store.files.atomic import write_text

POINT = "implement.deliver"
CHECK = "implement.check"
REVIEW = "implement.review"
CODE = "implement.code"
PASSED = "passed"
RESULT_LABELS = {"weak": "弱证据", "unverified": "未验证"}
# 发布读取的键 ← 编码输出的键
RELEASE_KEYS = {"title": "prTitle", "scope": "scope", "summary": "subject", "why": "why", "problem": "problem",
                "approach": "approach", "limitations": "limitations"}
SCHEMA = Path(__file__).with_name("deliver.schema.json")
ACTOR = "tightrein"
NOT_PASSED = ("weak", "unverified")


def run(runtime: Runtime, context: ImplementContext) -> Handoff:
    started = time.monotonic()
    if context.git is None or context.worktree is None or context.base_commit is None:
        raise ValueError("交付之前没有准备好 worktree 与基准 commit")
    git, base, worktree = context.git, context.base_commit, context.worktree
    diff_hash = git.diff_hash(base)
    stale = _stale(context, diff_hash)
    if stale is not None:
        point, reason = stale
        return _handoff(runtime, context, started, Status.FAILED, reason,
                        {"backTo": point, "diffHash": diff_hash, "skipped": None})
    check, review = context.last(CHECK), context.last(REVIEW)
    assert check is not None and review is not None  # _stale 已核对
    changes = collect(git, base)
    patch = runtime.workspace.step_file(context.issue.id, FileName(POINT, "diff", "patch"))
    write_text(patch, changes.patch)
    head = git.head()
    facts = _facts(context, worktree, head.commit or "", head.branch, base, diff_hash, changes, check, review, patch,
                   runtime)
    check_facts(POINT, facts, load_schema(SCHEMA))
    apply_event(runtime, context.issue.id, IssueEvent.DELIVER, actor=ACTOR,
                note=f"交给发布：{len(changes.files)} 个文件，diff {diff_hash[:12]}",
                updates={"branch": facts["branch"]})
    risk = "，命中高风险路径，合并需人工确认" if facts["highRisk"] else ""
    return _handoff(runtime, context, started, Status.PASSED,
                    f"交给发布：{len(changes.files)} 个文件{risk}", facts, changes)


def _stale(context: ImplementContext, diff_hash: str) -> tuple[str, str] | None:
    """自检与审查都要在当前这份改动上通过；不是的话回到最早需要重做的那一步。"""
    check, review = context.last(CHECK), context.last(REVIEW)
    if check is None or check.status is not Status.PASSED or check.facts.get("diffHash") != diff_hash:
        return CHECK, "自检没有在当前这份改动上通过，回到自检"
    if review is None or review.status is not Status.PASSED or review.facts.get("diffHash") != diff_hash:
        return REVIEW, "审查之后工作区又有改动，回到审查(不重新编码)"
    return None


def _facts(context: ImplementContext, worktree: Path, commit: str, head_branch: str | None, base: str, diff_hash: str,
           changes: Changes, check: Handoff, review: Handoff, patch: Path, runtime: Runtime) -> dict[str, Any]:
    """键名照发布读取的写(release/record.parse_delivery)：checks 为 {name, passed, detail}，review 为
    {round, conclusions}，release 为编码给出的提交与 PR 文字。"""
    paths = boundaries.high_risk(changes.paths, runtime.settings)
    runtime_items = check.facts.get("runtime") or []
    unverified = [f"{item['id']}：{RESULT_LABELS.get(item['result'], item['result'])}({item.get('reason') or '无说明'})"
                  for item in runtime_items if item["result"] in NOT_PASSED]
    unverified += [f"{item['item']}：{item['reason']}" for item in review.facts.get("unverified") or []]
    return {
        "branch": context.issue.branch or head_branch or "",
        "worktree": str(worktree),
        "commit": commit,
        "base": base,
        "diffHash": diff_hash,
        "changedFiles": [{"path": change.path, "added": change.added, "deleted": change.deleted,
                          "status": change.status} for change in changes.files],
        "checks": [*(_command(item) for item in check.facts.get("commands") or []),
                   *(_runtime_item(item) for item in runtime_items)],
        "acceptedFindings": [],
        "review": {"round": review.round, "conclusions": _conclusions(review)},
        "highRisk": bool(paths),
        "highRiskPaths": paths,
        "release": _release_text(context),
        "patch": str(patch),
        "unverified": unverified,
        "knowledgeSuggestions": [],
        "skipped": None,
        "rounds": _rounds(runtime, context),
    }


def _rounds(runtime: Runtime, context: ImplementContext) -> list[dict[str, Any]]:
    """每一轮编码、自检、审查的结论与阻断项(原文：性质、位置、说明)，读各轮落盘的交接。"""
    found = []
    for number in range(1, (context.round or 1) + 1):
        steps = {}
        for point in (CODE, CHECK, REVIEW):
            path = runtime.workspace.step_file(context.issue.id, FileName(point, "handoff", "json", round=number))
            if path.is_file():
                handoff = read(path)
                steps[point] = {"status": handoff.status.value, "summary": handoff.summary,
                                "blockers": [_blocker(item) for item in handoff.facts.get("blockers") or []]}
        if steps:
            found.append({"round": number, "steps": steps})
    return found


def _blocker(item: Mapping[str, Any]) -> str:
    where = f" {item['location']}" if item.get("location") else ""
    return f"[{item.get('category') or '?'}/{item.get('kind') or '?'}]{where}：{item.get('summary') or ''}"


def _command(item: Mapping[str, Any]) -> dict[str, Any]:
    passed = item["result"] == PASSED
    return {"name": str(item["name"]), "passed": passed,
            "detail": None if passed else f"`{item['command']}` {item['result']}：{item.get('reason') or ''}".strip()}


def _runtime_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """弱证据与未验证不阻断发布，但不能写成通过：结论写进名字，PR 描述里照样看得到。"""
    result = item["result"]
    if result in NOT_PASSED:
        name = f"{item['id']}({RESULT_LABELS[result]}：{item.get('reason') or '无说明'})"
        return {"name": name, "passed": True, "detail": None}
    return {"name": str(item["id"]), "passed": result == PASSED, "detail": item.get("reason")}


def _conclusions(review: Handoff) -> list[str]:
    lines = [review.summary]
    lines += [f"验收标准「{item['criterion']}」：{item['result']}({item['reason']})"
              for item in review.facts.get("acceptance") or []]
    return lines


def _release_text(context: ImplementContext) -> dict[str, str | None]:
    """编码给出的提交与 PR 文字(implement.code 的 release)，换成发布读取的键名。"""
    code = context.last(CODE)
    given = (code.facts.get("release") if code is not None else None) or {}
    return {target: given.get(source) for target, source in RELEASE_KEYS.items()}


def _handoff(runtime: Runtime, context: ImplementContext, started: float, status: Status, summary: str,
             facts: dict[str, Any], changes: Changes | None = None) -> Handoff:
    metrics = Metrics(duration_ms=int((time.monotonic() - started) * 1000),
                      files_changed=len(changes.files) if changes else None,
                      lines_changed=sum(item.added + item.deleted for item in changes.files) if changes else None)
    return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=status, summary=summary, facts=facts,
                   metrics=metrics, round=context.round, created_at=format_iso(runtime.clock.now()))
