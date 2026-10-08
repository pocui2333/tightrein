"""实施的流程：推进一个 Issue 一步(从检查点接着)，按已有信息跳过，超出范围时退回评估。

一次 `implement(runtime, issue)` 只做一件事：从落盘的产物推导下一步 → 做这一步 → 写这一步的 handoff.json(检查点，
带 worktree 的快照 commit) → 再推导一次，决定接着做、停在关卡、停下还是退回评估。不靠进度标记：中断后按各步的
handoff.json 与用户的决定接着做，没做完的那一步整个丢掉，worktree 退回上一个检查点。

下一步怎样推导(`decide`)：

| 最后完成的一步 | 结论 | 下一步 |
|---|---|---|
| 没有 | — | 准备 |
| 任一步 | 通过 | 按 STEPS 的顺序；定案之后开新一轮编码，编码 → 自检 → 审查同一轮，审查通过后交付 |
| 任一步 | 待决定 | 有了用户的新决定就重做这一步(由它取用决定)，否则继续等 |
| 编码、自检、审查 | 没通过 | 越界：第一次撤回重做这一轮，再越界停下；检查没跑起来停下；不通过项按性质只处理最靠前的一类：设计问题停下、需要用户等决定、方案缺口重出方案、局部问题交回编码(轮数上限 controls.implement.review.rounds，连续两轮阻断项相同或 diff 没变即停) |
| 方案 | 没通过 | 一个 PR 放不下：退回评估拆分(SPLIT_BACK)；其余停下 |
| 定案 | 没通过(用户否决) | 按原因重出方案，上限 controls.implement.design.rounds |
| 停下之后 | 用户给了新决定 | approve：编码类的接着开一轮，其余重做那一步；reject：重出方案 |

回归或重开后的又一次修复从准备开始：开始前把上一次尝试的交接挪进 `attempt_<次>/`(assess/issue/attempts)。
每个 Issue 的用量超过 resources.issueTokens 即停下。停在关卡写 `90-issue-pending.md`，停下写 `90-issue-failure.md`。
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from tightrein.assess.issue import attempts
from tightrein.assess.issue import files as issue_files
from tightrein.assess.issue.create import split_back
from tightrein.assess.issue.stages import IMPLEMENT, stage_for
from tightrein.assess.issue.transitions import IssueEvent, IssueStatus, apply_event
from tightrein.implement.check.findings import DESIGN as DESIGN_ISSUE
from tightrein.implement.check.findings import NEEDS_USER, PLAN_GAP, Finding, first_category, from_facts, keys
from tightrein.implement.context import STEPS, Decision, ImplementContext, load
from tightrein.implement.prepare import workspace
from tightrein.protocol import documents, recovery
from tightrein.protocol.handoff import Handoff, Status, write
from tightrein.protocol.limits import no_progress
from tightrein.protocol.naming import FileName, format_iso
from tightrein.protocol.records import versions
from tightrein.protocol.recovery import CHECKPOINT_COMMIT
from tightrein.store.tables import issues

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

PREPARE, LOCATE, DESIGN, APPROVE, CODE, CHECK, REVIEW, DELIVER = STEPS
STAGE = "implement"
ROUND_POINTS = (CODE, CHECK, REVIEW)
ACTOR = "tightrein"
# 各小步骤的入口模块：统一为 run(runtime, context) -> Handoff；按需导入，没走到的步骤不加载
RUNNERS = {
    PREPARE: "tightrein.implement.prepare.workspace",
    LOCATE: "tightrein.implement.locate.locate",
    DESIGN: "tightrein.implement.design.design",
    APPROVE: "tightrein.implement.approve.approve",
    CODE: "tightrein.implement.code.code",
    CHECK: "tightrein.implement.check.check",
    REVIEW: "tightrein.implement.review.review",
    DELIVER: "tightrein.implement.deliver.deliver",
}
GATE_DESIGN = "design"  # 方案确认(可配置的关卡)
GATE_DECISION = "needs_decision"  # 需要拍板(固定人工)
CONTINUE = "tightrein approve {issue} --note \"<补充说明>\""
RERUNS = "reruns"  # 同一步同一轮没通过后重跑的次数(implement.py 记)
REVIEW_RERUNS = 1  # 审查没给出结果时只重跑审查的次数
DELIVER_RERUNS = 1  # 交付发现自检、审查后又有改动时回去重做的次数


class Kind(StrEnum):
    RUN = "run"  # 做这一步
    WAIT = "wait"  # 停在关卡等用户
    STOP = "stop"  # 停下交人
    SPLIT = "split"  # 一个 PR 放不下，退回评估拆分
    DONE = "done"  # 交付完成，交给发布


@dataclass
class StepOutcome:
    subject: str
    point: str
    status: Status
    summary: str
    next_point: str | None


@dataclass(frozen=True)
class Next:
    kind: Kind
    point: str
    round: int | None = None
    reason: str = ""
    code: str = ""  # 停下原因的代码，写进 Issue 的 hold
    tried: tuple[str, ...] = ()
    advice: str = ""
    gate: dict[str, Any] = field(default_factory=dict)
    parts: tuple[str, ...] = ()  # 退回评估时拆成的几个 Issue 的说明(第一行为标题)


class NotImplementable(Exception):
    """Issue 不在待修或实施中(或已手动接管)，不能推进。"""


# 公共函数


def implement(runtime: Runtime, issue: str) -> StepOutcome:
    record = issues.get(runtime.conn, issue)
    if record is None:
        raise LookupError(f"没有 Issue {issue}")
    if record.held_by is not None or record.status == IssueStatus.HELD:
        raise NotImplementable(f"Issue {issue} 已由 {record.held_by} 手动接管，tightrein 不再碰它")
    if record.status == IssueStatus.TODO:
        # 回归、重开后的又一次修复：先把上一次尝试的交接挪进归档，各步不沿用上一次的「已完成」(attempts.py)
        if attempts.archive(runtime.workspace, record) is not None:
            _emit(runtime, issue, STAGE, f"第 {attempts.current(record)} 次修复：上一次的交接已归档")
        apply_event(runtime, issue, IssueEvent.START, actor=ACTOR)
    elif stage_for(record.status) != IMPLEMENT:
        raise NotImplementable(f"Issue {issue} 当前为 {record.status}，只有待修与实施中的 Issue 能推进")
    context = load(runtime, issue)
    if runtime.agents.budget.exceeded(issue):
        used = runtime.agents.budget.used(issue)
        return _settle(runtime, context, Next(Kind.STOP, _current(context), reason=(
            f"用量已达上限：{used:.0f} / {runtime.agents.budget.limit:.0f} token(缓存读取按权重计)"), code="budget",
            advice="缩小 Issue 范围或拆分后重来；确实需要时调高 resources.issueTokens"), None)
    found = decide(runtime, context)
    if found.kind is not Kind.RUN:
        return _settle(runtime, context, found, None)
    _rewind(runtime, context)
    if found.round is not None:
        context.round = found.round
    handoff = _run(runtime, context, found.point, previous=context.last(found.point))
    context.latest[handoff.point] = handoff
    return _settle(runtime, context, decide(runtime, context), handoff)


def decide(runtime: Runtime, context: ImplementContext) -> Next:
    """从落盘的交接与用户的决定推导下一步(见模块说明的表)。"""
    done = _history(runtime, context.issue.id)
    if not done:
        return Next(Kind.RUN, PREPARE)
    last = done[-1]
    fresh = _decisions_after(context, last.created_at)
    if last.status is Status.PASSED:
        return _after_passed(context, last)
    if last.status is Status.PENDING:
        if fresh:
            return Next(Kind.RUN, last.point, round=last.round)
        return Next(Kind.WAIT, last.point, round=last.round, reason=last.summary,
                    gate=dict(last.facts.get("gate") or {}))
    if fresh:
        return _retry(context, last, fresh[-1])
    return _dispatch(runtime, context, done, last)


# 推导下一步


def _after_passed(context: ImplementContext, last: Handoff) -> Next:
    if last.point == DELIVER:
        return Next(Kind.DONE, DELIVER)
    if last.point == APPROVE:
        return Next(Kind.RUN, CODE, round=_next_round(context))
    following = STEPS[STEPS.index(last.point) + 1]
    return Next(Kind.RUN, following, round=last.round if following in ROUND_POINTS else None)


def _retry(context: ImplementContext, last: Handoff, decision: Decision) -> Next:
    """停下之后用户给了新决定：reject 重出方案(原因已记进用户的决定)；approve 接着做。"""
    if decision.verdict == "reject" and last.point in (APPROVE, *ROUND_POINTS):
        return Next(Kind.RUN, DESIGN)
    if last.point in ROUND_POINTS:
        return Next(Kind.RUN, CODE, round=_next_round(context))
    return Next(Kind.RUN, last.point, round=last.round)


def _dispatch(runtime: Runtime, context: ImplementContext, done: Sequence[Handoff], last: Handoff) -> Next:
    if last.point == DESIGN:
        if last.facts.get("reason") == "oversize":
            parts = tuple(_part_text(item) for item in (last.facts.get("splitBack") or {}).get("parts") or [])
            return Next(Kind.SPLIT, DESIGN, reason=last.summary, parts=parts)
        return _stop(last, code=str(last.facts.get("reason") or "design_failed"),
                     advice="补充说明后接着做，或缩小 Issue 范围", tried=last.facts.get("problems") or ())
    if last.point == APPROVE:
        return _replan(runtime, context, last, "方案被否决")
    back = last.facts.get("backTo")
    if last.point == DELIVER and back in ROUND_POINTS and int(last.facts.get(RERUNS, 0)) < DELIVER_RERUNS:
        return Next(Kind.RUN, str(back), round=_next_round(context) - 1)  # 自检或审查后又有改动：只回到那一步
    if last.point in (PREPARE, LOCATE, DELIVER):
        advice = ("修正项目的检查命令、准备命令或环境后接着做" if last.facts.get("reason") == "config_error"
                  else "看交接中的原因，处理后接着做")
        return _stop(last, code=str(last.facts.get("reason") or f"{last.point.split('.')[1]}_failed"), advice=advice)
    return _blocked(runtime, context, done, last)


def _blocked(runtime: Runtime, context: ImplementContext, done: Sequence[Handoff], last: Handoff) -> Next:
    """编码、自检、审查没通过：按越界、没跑起来、不通过项的性质分派。"""
    if last.facts.get("violations"):
        if not last.facts.get("redo"):
            return Next(Kind.RUN, CODE, round=last.round)
        return _stop(last, code="boundary", advice="越界两次：看交接中越界的文件，调整方案或手动处理")
    findings = from_facts(last.facts)
    category = first_category(findings)
    if category is None:
        if last.point == REVIEW and last.facts.get("callFailed") and int(last.facts.get(RERUNS, 0)) < REVIEW_RERUNS:
            return Next(Kind.RUN, REVIEW, round=last.round)  # 审查没给出结果：只重跑审查，不重新编码
        return _stop(last, code=f"{last.point.split('.')[1]}_failed", advice="看交接中的原因，处理后接着做")
    chosen = [item for item in findings if item.category == category]
    if category == DESIGN_ISSUE:
        return _stop(last, code="design_issue", reason="根因在设计本身：" + "；".join(item.text() for item in chosen),
                     advice="决定是找代码作者讨论还是按设计层面修复(同意时 approve 并在 --note 写明)")
    if category == NEEDS_USER:
        return Next(Kind.WAIT, last.point, round=last.round, reason="；".join(item.text() for item in chosen),
                    gate=_needs_user_gate(context, chosen))
    if category == PLAN_GAP:
        return _replan(runtime, context, last, "编码中发现方案没覆盖")
    return _local(runtime, context, done, last, findings)


def _local(runtime: Runtime, context: ImplementContext, done: Sequence[Handoff], last: Handoff,
           findings: Sequence[Finding]) -> Next:
    """局部问题交回编码；轮数用完或没有进展就停。"""
    rounds = int(runtime.settings.control(REVIEW, "rounds"))
    first = _cycle_first_round(context, done)
    current = last.round or 1
    tried = tuple(f"第 {item.round} 轮 {item.point}：{item.summary}" for item in done
                  if item.point in ROUND_POINTS and item.status is Status.FAILED and (item.round or 0) >= first)
    if current - first + 1 > rounds:
        return _stop(last, code="rounds_exceeded", reason=f"交回修改 {rounds} 轮仍未通过：{last.summary}", tried=tried,
                     advice="看最后一轮的不通过项，补充说明后接着做，或缩小范围")
    previous = _failing(done, current - 1) if current > first else None
    if previous is not None and no_progress(keys(from_facts(previous.facts)), keys(findings),
                                            _diff(done, current - 1), _diff(done, current)):
        return _stop(last, code="no_progress", reason=f"连续两轮没有进展(阻断项相同或改动没变)：{last.summary}",
                     tried=tried, advice="换个思路：补充说明后接着做，或否决方案重出")
    return Next(Kind.RUN, CODE, round=current + 1)


def _replan(runtime: Runtime, context: ImplementContext, last: Handoff, why: str) -> Next:
    """重出方案(复用上次通过的定位结论)；超过 controls.implement.design.rounds 就停下。"""
    design = context.last(DESIGN)
    used = int(design.facts.get("version", 1)) - 1 if design is not None else 0
    limit = int(runtime.settings.control(DESIGN, "rounds"))
    if used >= limit:
        return _stop(last, code="replans_exceeded", reason=f"{why}，方案已重出 {used} 次：{last.summary}",
                     advice="补充说明后接着做，或缩小 Issue 范围")
    return Next(Kind.RUN, DESIGN)


def _stop(last: Handoff, *, code: str, advice: str, reason: str | None = None,
          tried: Sequence[str] = ()) -> Next:
    return Next(Kind.STOP, last.point, round=last.round, reason=reason or last.summary, code=code,
                tried=tuple(map(str, tried)), advice=advice)


def _part_text(part: dict[str, Any]) -> str:
    """拆出的一个 Issue 的说明：第一行为标题，其后为目标与验收标准。"""
    criteria = "\n".join(f"- {item}" for item in part.get("acceptance") or [])
    return f"{part['title']}\n\n{part['goal']}\n\n验收标准：\n{criteria}"


def _needs_user_gate(context: ImplementContext, chosen: Sequence[Finding]) -> dict[str, Any]:
    issue = context.issue.id
    return {
        "decision": "下面的问题需要你拍板：\n\n" + "\n".join(f"- {item.text()}" for item in chosen),
        "options": [f"接着修(在 --note 写明怎么处理)：tightrein approve {issue} --note \"<决定>\"",
                    f"按你的意见重出方案：tightrein reject {issue} --note \"<要求>\""],
        "recommendation": "按问题说明给出决定",
        "reason": "这类问题(越界、禁改文件、改动量收敛后仍超上限等)不能由编码自行纠正",
        "ifNot": "Issue 停在这一步，不会自动继续",
        "command": f"tightrein approve {issue} --note \"<决定>\"",
    }


# 执行一步与收尾


def _run(runtime: Runtime, context: ImplementContext, point: str, *, previous: Handoff | None) -> Handoff:
    started = time.monotonic()
    module = importlib.import_module(RUNNERS[point])
    runner: Callable[[Runtime, ImplementContext], Handoff] = module.run
    handoff = runner(runtime, context)
    return _record(runtime, context, handoff, int((time.monotonic() - started) * 1000), previous)


def _record(runtime: Runtime, context: ImplementContext, handoff: Handoff, elapsed_ms: int,
            previous: Handoff | None) -> Handoff:
    """补上程序统计的部分(检查点 commit、轮次、重跑次数、耗时、时间、版本)后落盘：写好才算这一步完成。"""
    facts = dict(handoff.facts)
    if CHECKPOINT_COMMIT not in facts and context.git is not None:
        facts[CHECKPOINT_COMMIT] = workspace.snapshot(context.git)
    # status 与知识库按固定的键读：没跳过的为 null，程序步骤没有建议沉淀的为空列表
    facts.setdefault("skipped", None)
    facts.setdefault("knowledgeSuggestions", [])
    number = context.round if handoff.round is None and handoff.point in ROUND_POINTS else handoff.round
    # 同一步同一轮没通过后又跑一次：记下第几次重跑，分派时据此不无限重跑
    again = previous is not None and previous.round == number and previous.status is Status.FAILED
    facts[RERUNS] = int(previous.facts.get(RERUNS, 0)) + 1 if again and previous is not None else 0
    metrics = handoff.metrics if handoff.metrics.duration_ms is not None \
        else replace(handoff.metrics, duration_ms=elapsed_ms)
    found = handoff.versions
    if found.settings is None:
        found = versions(runtime.tool.root, runtime.settings.hash, prompt_hash=found.prompt, tool=found.tool,
                         tool_version=found.tool_version, model=found.model)
    handoff = replace(handoff, facts=facts, metrics=metrics, versions=found, round=number,
                      created_at=handoff.created_at or format_iso(runtime.clock.now()))
    write(runtime.workspace.step_file(handoff.subject, FileName(handoff.point, "handoff", "json", round=handoff.round)),
          handoff)
    runtime.events.emit(run=runtime.run, subject=handoff.subject, point=handoff.point, kind="effect",
                        summary=f"{handoff.status.value}：{handoff.summary}")
    if handoff.status is Status.PASSED:
        runtime.agents.breaker.object_progressed(handoff.subject)
    return handoff


def _settle(runtime: Runtime, context: ImplementContext, found: Next, ran: Handoff | None) -> StepOutcome:
    issue = context.issue.id
    point = ran.point if ran is not None else found.point
    summary = ran.summary if ran is not None else found.reason
    if found.kind is Kind.RUN:
        updates: dict[str, Any] = {"step": found.point, "round": found.round, "gate": None}
        if ran is not None and ran.point == PREPARE:
            updates["branch"] = ran.facts.get("branch")
        _mark(runtime, issue, updates)
        return StepOutcome(issue, point, Status.PASSED, summary, found.point)
    if found.kind is Kind.WAIT:
        gate = found.gate
        if gate:  # 没给关卡内容的步骤(自检、审查)已自己写了待审核文档
            documents.pending(runtime, issue, point=found.point, decision=str(gate.get("decision") or found.reason),
                              options=[str(item) for item in gate.get("options") or []],
                              recommendation=str(gate.get("recommendation") or ""),
                              reason=str(gate.get("reason") or ""), if_not=str(gate.get("ifNot") or ""),
                              command=str(gate.get("command") or f"tightrein approve {issue}"))
        _mark(runtime, issue, {"step": found.point, "round": found.round,
                               "gate": GATE_DESIGN if found.point == APPROVE else GATE_DECISION})
        _emit(runtime, issue, found.point, f"停在关卡等用户：{found.reason}")
        return StepOutcome(issue, point, Status.PENDING, found.reason, None)
    if found.kind is Kind.SPLIT:
        created = split_back(runtime, issue, found.parts)
        _emit(runtime, issue, found.point, f"退回评估拆成 {'、'.join(created)}：{found.reason}")
        return StepOutcome(issue, point, Status.PASSED, f"{found.reason}：拆成 {'、'.join(created)}", None)
    if found.kind is Kind.DONE:
        record = issues.get(runtime.conn, issue)
        if record is not None and record.status == IssueStatus.IMPLEMENTING:  # 交付一步通常已转给发布
            apply_event(runtime, issue, IssueEvent.DELIVER, actor=ACTOR)
        return StepOutcome(issue, point, Status.PASSED, summary, None)
    documents.failure(runtime, issue, point=found.point, reason=found.reason, tried=list(found.tried),
                      advice=found.advice, command=CONTINUE.format(issue=issue))
    apply_event(runtime, issue, IssueEvent.FAIL, reason=found.code, actor=ACTOR, note=found.reason,
                updates={"step": found.point, "round": found.round})
    _emit(runtime, issue, found.point, f"停下：{found.reason}")
    return StepOutcome(issue, point, Status.FAILED, found.reason, None)


def _rewind(runtime: Runtime, context: ImplementContext) -> None:
    """没做完的那一步整个丢掉：worktree 与上一个检查点不一致时退回去，不续接半截状态。"""
    if context.git is None:
        return
    commit = recovery.checkpoint_commit(runtime.workspace, context.issue.id)
    if commit is None or workspace.current_tree(context.git) == workspace.tree_of(context.git, commit):
        return
    workspace.restore(context.git, commit)
    _emit(runtime, context.issue.id, STAGE, f"worktree 有没做完的改动，已退回检查点 {commit[:12]}")


def _mark(runtime: Runtime, issue: str, updates: dict[str, Any]) -> None:
    """不改状态的进度(所在步骤、轮次、关卡、分支)：写进 Issue 记录，供 watch 与 status 显示。"""
    record = issues.get(runtime.conn, issue)
    if record is None:
        raise LookupError(f"没有 Issue {issue}")
    changed = replace(record, **{key: value for key, value in updates.items() if key != "branch" or value})
    if changed != record:
        issue_files.write(runtime, changed)


def _emit(runtime: Runtime, issue: str, point: str, summary: str) -> None:
    runtime.events.emit(run=runtime.run, subject=issue, point=point, kind="decision", summary=summary)


# 读取产物


def _history(runtime: Runtime, issue: str) -> list[Handoff]:
    """实施各步已完成的交接，按完成的先后。"""
    return [item.handoff for item in recovery.checkpoints(runtime.workspace, issue) if item.handoff.point in STEPS]


def _decisions_after(context: ImplementContext, since: str | None) -> list[Decision]:
    return [item for item in context.decisions if item.at > (since or "")]


def _next_round(context: ImplementContext) -> int:
    """新一轮编码的轮次：接着已有的编号往下，不覆盖前面各轮的交接。"""
    found = [handoff.round or 0 for point in ROUND_POINTS if (handoff := context.last(point)) is not None]
    return max(found, default=0) + 1


def _current(context: ImplementContext) -> str:
    return context.issue.step if context.issue.step in STEPS else STAGE


def _cycle_first_round(context: ImplementContext, done: Sequence[Handoff]) -> int:
    """这一版方案(或用户最近一次决定)之后的第一轮：轮数上限按它算。"""
    design = context.last(DESIGN)
    planned = (design.created_at or "") if design is not None else ""
    since = max([planned, *(item.at for item in context.decisions)])
    rounds = [item.round or 1 for item in done if item.point == CODE and (item.created_at or "") >= since]
    return min(rounds, default=1)


def _failing(done: Sequence[Handoff], number: int) -> Handoff | None:
    found = [item for item in done if item.point in ROUND_POINTS and item.round == number
             and item.status is Status.FAILED]
    return found[-1] if found else None


def _diff(done: Sequence[Handoff], number: int) -> str | None:
    found = [item for item in done if item.point == CODE and item.round == number]
    value = found[-1].facts.get("diffHash") if found else None
    return str(value) if value else None
