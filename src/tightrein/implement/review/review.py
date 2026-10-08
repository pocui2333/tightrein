"""审查(implement.review)：轻量审查都做，高风险另加深度审查(implement.review.deep，盲审，与编码不同的模型)。

- 改动没变就不重做：上次已通过且与提交无关的 diff 哈希(protocol.git.diff_hash，比较基准为 review_base)相同，直接
  沿用；合并主干后只在主干改到同一文件时哈希才变，才需要重新审查；
- 修正轮次只审这一轮又改了的文件与上一轮的问题，不重审全部；上一轮审查没有返回结果(调用失败)时这一轮照常审全部；
- 先轻量后深度：轻量没通过就不跑深度，省一次调用；
- 审查意见的程序过滤：类型必须在该模式允许的范围内(轻量 6 类；深度另加 authz、data-structure、contract)；必须有
  `文件:行号` 且位置真实存在；必须有触发条件；不合格的丢弃并记入 discardedFindings，不计为不通过；
- 逐条核对验收标准：判为不满足而审查没给出对应阻断项的，按方案缺口记一条；
- 只能在部署后确认的验收标准(assess/issue/body.post_deploy)不交给审查判断；模型仍给了结论的丢掉，不因它不通过、
  不因它等用户：它由发布的验收按关联问题的出现确认；
- 「无法判断」交给用户：有验收标准判为 unknown 时停在待决定；用户通过后视为通过、判断原文记入 unverified，
  不重跑编码与审查；
- 审查没有返回结果时交出失败且标 callFailed：下一轮只重跑审查，不重新编码(由 implement.py 分派)；
- 与本 Issue 无关、顺带发现的缺陷写进 facts.incidentalFindings(结构照 collect/incidental/finding.schema.json)，
  发现的 commit 取 facts.baseCommit(collect.incidental 的 points 按此读)。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tightrein.agents.result import CallStatus
from tightrein.assess.issue.body import post_deploy
from tightrein.implement.check.changes import Changes, changed_since, collect, file_states, select_files
from tightrein.implement.check.findings import LOCAL, PLAN_GAP, Finding, first_category
from tightrein.implement.context import ImplementContext
from tightrein.implement.design.risk import Changed, apply_risk, conditions, to_facts
from tightrein.implement.locate.brief import location_problem
from tightrein.implement.prompts import review as prompts
from tightrein.implement.prompts.common import Ask, Usage, ask, failure_text
from tightrein.protocol.documents import pending
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.naming import format_iso, parse_iso
from tightrein.protocol.runtime import Runtime

POINT = "implement.review"
DEEP_POINT = "implement.review.deep"
CHECK = "implement.check"
DESIGN = "implement.design"
LIGHT = "light"
DEEP = "deep"
SCHEMA = Path(__file__).with_name("review.schema.json")
BASE_KINDS = frozenset({"root-cause-unfixed", "caller-broken", "hardcode", "new-error-path", "requirement-unmet",
                        "requirement-reduced"})
ALLOWED_KINDS = {LIGHT: BASE_KINDS, DEEP: BASE_KINDS | {"authz", "data-structure", "contract"}}
REQUIREMENT_KINDS = frozenset({"requirement-unmet", "requirement-reduced"})
UNKNOWN = "unknown"
FAIL = "fail"
APPROVE = "approve"
MISJUDGED = "misjudged"
FALSE_BLOCK = "false_block"


@dataclass
class ModeReview:
    mode: str
    called: bool  # 模型给出了结构化结果
    error: str | None = None
    kept: list[Finding] = field(default_factory=list)
    discarded: list[dict[str, Any]] = field(default_factory=list)
    acceptance: list[dict[str, Any]] = field(default_factory=list)
    unverified: list[dict[str, Any]] = field(default_factory=list)
    misjudged: list[dict[str, Any]] = field(default_factory=list)  # 上一轮的阻断项被这一轮判为误报
    suggestions: list[str] = field(default_factory=list)
    incidental: list[dict[str, Any]] = field(default_factory=list)  # 任务外发现(结构照 finding.schema.json)

    @property
    def unknown(self) -> list[dict[str, Any]]:
        return [item for item in self.acceptance if item["result"] == UNKNOWN]

    @property
    def passed(self) -> bool:
        return self.called and not self.kept and not self.unknown


def run(runtime: Runtime, context: ImplementContext) -> Handoff:
    started = time.monotonic()
    if context.git is None or context.worktree is None or context.base_commit is None:
        raise ValueError("审查之前没有准备好 worktree 与基准 commit")
    git, base, worktree = context.git, context.base_commit, context.worktree
    diff_hash = git.diff_hash(base)
    previous = context.last(POINT)
    if previous is not None and previous.facts.get("diffHash") == diff_hash:
        if previous.status is Status.PASSED:
            return _reuse(runtime, context, previous, f"改动没变，沿用上次审查结论：{previous.summary}")
        if previous.status is Status.PENDING:
            decided = _user_decision(runtime, context, previous)
            if decided is not None:
                return decided
    changes = collect(git, base)
    states = file_states(worktree, changes.paths)
    incremental = _incremental(previous)
    files = changed_since(previous.facts.get("fileStates") or {}, states) if incremental and previous else None
    plan = _facts(context, DESIGN)
    risk = apply_risk([Changed(path, lines.added, lines.removed) for path, lines in changes.lines.items()], plan,
                      context, runtime.settings)
    check = _facts(context, CHECK)
    results = prompts.results_text(check)
    usage = Usage()
    previous_blockers = [Finding.from_json(item).text() for item in (previous.facts.get("blockers") or [])] \
        if incremental and previous else []
    light_review = _review(runtime, context, LIGHT, prompts.light(
        context, plan=plan, diff=changes.patch if files is None else select_files(changes.patch, files),
        results=results, hints=check.get("hardcodeHints") or [], previous=previous_blockers, files=files),
        usage, worktree)
    reviews = [light_review]
    if light_review.passed and risk.high:
        reviews.append(_review(runtime, replace(context, risk=risk), DEEP,
                               prompts.deep(context, diff=changes.patch, results=results, risk=risk), usage, worktree))
    return _handoff(runtime, context, started, base, diff_hash, states, changes, files, reviews, risk, usage)


def filter_findings(output: Mapping[str, Any], worktree: Path, mode: str) -> tuple[list[Finding], list[dict[str, Any]]]:
    kept: list[Finding] = []
    discarded: list[dict[str, Any]] = []
    for item in output["blockers"]:
        reason = discard_reason(item, worktree, mode)
        if reason is None:
            kept.append(Finding(_check(mode), item["kind"], item["location"],
                                f"{item['problem']}(根源：{item['rootCause']})", item["category"], item["trigger"]))
        else:
            discarded.append({"finding": dict(item), "reason": reason})
    return kept, discarded


def discard_reason(item: Mapping[str, Any], worktree: Path, mode: str) -> str | None:
    if item["kind"] not in ALLOWED_KINDS[mode]:
        return f"问题类型 {item['kind']} 不在{'深度' if mode == DEEP else '轻量'}审查允许的范围内"
    if not item.get("location"):
        return "没有给出「文件路径:行号」"
    problem = location_problem(worktree, str(item["location"]))
    if problem is not None:
        return f"位置不存在：{problem}"
    if not (item.get("trigger") or "").strip():
        return "没有给出触发条件"
    return None


def _review(runtime: Runtime, context: ImplementContext, mode: str, variables: Mapping[str, str], usage: Usage,
            worktree: Path) -> ModeReview:
    point = POINT if mode == LIGHT else DEEP_POINT
    result = ask(runtime, context, Ask(point, variables, SCHEMA, conditions=conditions(context.risk),
                                       round=context.round), usage)
    if result.status is not CallStatus.OK or result.output is None:
        return ModeReview(mode, called=False, error=failure_text(result))
    kept, discarded = filter_findings(result.output, worktree, mode)
    acceptance = [dict(item) for item in result.output["acceptance"] if not post_deploy(str(item["criterion"]))]
    if not any(finding.kind in REQUIREMENT_KINDS for finding in kept):
        kept += [Finding(_check(mode), "requirement-unmet", f"验收标准：{item['criterion']}", item["reason"], PLAN_GAP)
                 for item in acceptance if item["result"] == FAIL]
    misjudged = [dict(item) for item in result.output["previousBlockers"] if item["verdict"] == MISJUDGED]
    return ModeReview(mode, True, None, kept, discarded, acceptance, [dict(item) for item in result.output["unverified"]],
                      misjudged, [str(item) for item in result.output["knowledgeSuggestions"]],
                      [dict(item) for item in result.output["incidentalFindings"]])


def _handoff(runtime: Runtime, context: ImplementContext, started: float, base: str, diff_hash: str,
             states: Mapping[str, str], changes: Changes, files: Sequence[str] | None, reviews: Sequence[ModeReview],
             risk: Any, usage: Usage) -> Handoff:
    blockers = [finding for review in reviews for finding in review.kept]
    unknown = [item for review in reviews for item in review.unknown]
    failed_call = next((review for review in reviews if not review.called), None)
    facts: dict[str, Any] = {
        "base": base,
        "baseCommit": base,
        "diffHash": diff_hash,
        "fileStates": dict(states),
        "modes": [review.mode for review in reviews],
        "incremental": files is not None,
        "reviewedFiles": list(changes.paths if files is None else files),
        "acceptance": [item for review in reviews for item in review.acceptance],
        "blockers": [finding.to_json() for finding in blockers],
        "firstCategory": first_category(blockers),
        "discardedFindings": [item for review in reviews for item in review.discarded],
        "unverified": [item for review in reviews for item in review.unverified],
        "awaitingUser": unknown,
        "callFailed": failed_call is not None,
        "risk": to_facts(risk),
        "misjudged": _misjudged([item for review in reviews for item in review.misjudged]),
        "knowledgeSuggestions": [item for review in reviews for item in review.suggestions],
        "incidentalFindings": [item for review in reviews for item in review.incidental],
        "skipped": None,
    }
    if failed_call is not None:
        status, summary = Status.FAILED, f"{failed_call.mode} 审查没有返回结果：{failed_call.error}；下一轮只重跑审查"
    elif blockers:
        status, summary = Status.FAILED, f"审查有 {len(blockers)} 个阻断项(最靠前的一类：{first_category(blockers)})"
    elif unknown:
        status, summary = Status.PENDING, f"有 {len(unknown)} 条验收标准审查无法判断，等用户判断"
        _pending_document(runtime, context, unknown)
    else:
        status, summary = Status.PASSED, "审查通过：" + "、".join(f"{review.mode}" for review in reviews)
    metrics = usage.metrics(duration_ms=int((time.monotonic() - started) * 1000),
                            produced={"blockers": len(blockers),
                                      "discarded": len(facts["discardedFindings"])},
                            passed=sum(1 for item in facts["acceptance"] if item["result"] == "pass"),
                            failed=len(blockers))
    return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=status, summary=summary, facts=facts,
                   metrics=metrics, round=context.round, created_at=format_iso(runtime.clock.now()),
                   versions=usage.versions(runtime))


def _user_decision(runtime: Runtime, context: ImplementContext, previous: Handoff) -> Handoff | None:
    """上次停在「无法判断」，之后用户给了判断：通过即视为通过(判断原文记入 unverified)，不通过即为局部问题。"""
    since = parse_iso(previous.created_at) if previous.created_at else None
    decision = next((item for item in reversed(context.decisions) if item.point == POINT
                     and (since is None or parse_iso(item.at) >= since)), None)
    if decision is None:
        return None
    facts = {**previous.facts, "misjudged": None}  # 误判已记在等待时的交接里
    if decision.verdict == APPROVE:
        facts["unverified"] = [*facts.get("unverified", []),
                               *({"item": item["criterion"], "reason": f"用户判断：{decision.note or '通过'}"}
                                 for item in facts.get("awaitingUser", []))]
        facts["awaitingUser"] = []
        return replace(previous, status=Status.PASSED, summary="用户判断通过了审查无法判断的验收标准", facts=facts,
                       run=runtime.run, round=context.round, created_at=format_iso(runtime.clock.now()))
    blocker = Finding("review", "user-judgement", None, f"用户判断不满足：{decision.note}", LOCAL)
    facts["blockers"] = [blocker.to_json()]
    facts["firstCategory"] = LOCAL
    facts["awaitingUser"] = []
    return replace(previous, status=Status.FAILED, summary="用户判断不满足验收标准", facts=facts, run=runtime.run,
                   round=context.round, created_at=format_iso(runtime.clock.now()))


def _reuse(runtime: Runtime, context: ImplementContext, previous: Handoff, summary: str) -> Handoff:
    """沿用的交接不再带误判与任务外发现：它们已记在上次的交接里，复盘与采集不重复计。"""
    return replace(previous, run=runtime.run, round=context.round, summary=summary,
                   facts={**previous.facts, "skipped": "改动没变，沿用上次审查结论", "misjudged": None,
                          "incidentalFindings": []},
                   created_at=format_iso(runtime.clock.now()))


def _incremental(previous: Handoff | None) -> bool:
    """上一轮审查给出了阻断项(不是调用失败)时，这一轮只审又改了的文件。"""
    return previous is not None and previous.status is Status.FAILED and not previous.facts.get("callFailed") \
        and bool(previous.facts.get("fileStates"))


def _misjudged(items: Sequence[Mapping[str, Any]]) -> dict[str, str] | None:
    """上一轮报的阻断项这一轮核对后判为误报：复盘据此记一条误判(false_block)。"""
    if not items:
        return None
    detail = "；".join(f"{item.get('location') or '-'} {item['kind']}：{item['reason']}" for item in items)
    return {"kind": FALSE_BLOCK, "point": POINT, "detail": detail}


def _facts(context: ImplementContext, point: str) -> dict[str, Any]:
    found = context.last(point)
    return dict(found.facts) if found is not None else {}


def _check(mode: str) -> str:
    return POINT if mode == LIGHT else DEEP_POINT


def _pending_document(runtime: Runtime, context: ImplementContext, unknown: Sequence[Mapping[str, Any]]) -> None:
    subject = context.issue.id
    listing = "\n".join(f"- {item['criterion']}：{item['reason']}" for item in unknown)
    pending(runtime, subject, point=POINT, decision="审查无法判断下列验收标准是否满足，请判断",
            options=["满足(通过)", "不满足(用 reject 写明哪里不满足)"], recommendation="按原因中写的方法核对后判断",
            reason=listing, if_not="改动不能交付",
            command=f"tightrein approve {subject} --note \"<判断>\" 或 tightrein reject {subject} --note \"<哪里不满足>\"")
