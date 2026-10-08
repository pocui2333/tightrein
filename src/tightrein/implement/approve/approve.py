"""定案(implement.approve)：真风险或超出自动确认门槛时等用户确认方案，其余自动。

- 确认绑定方案哈希：交接记下所确认方案的 planHash；方案一重出哈希就变，旧的确认随之失效，编码前再核对一次；
- 自动确认的条件(boundaries.gates.design 为 auto 时)：没有需要拍板的点、三类标记、高风险文件、迁移、新增依赖、
  删除文件，风险判定不是高风险，预估在 boundaries.autoApprove 门槛内，前端设计说明与方案没有冲突；不满足时列出
  全部原因停在关卡，同一份方案的原因只记一次事件；
- 用户的决定由命令行经 `record` 写进 Issue(来源、时间、原文)，之后每次方案与编码都带上；approve 即通过，
  reject 必带原因，implement.py 按原因重出方案(上限 controls.implement.design.rounds)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from tightrein.assess.issue import files as issue_files
from tightrein.implement.context import DECISIONS, Decision
from tightrein.protocol.boundaries import gate_is_auto
from tightrein.protocol.handoff import Handoff, Metrics, Status
from tightrein.settings.load import Settings
from tightrein.store.tables import issues, operations

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime

POINT = "implement.approve"
DESIGN = "implement.design"
GATE = "design"
APPROVE = "approve"
REJECT = "reject"
FLAGS = ("design", "dataStructure", "publicContract")


def run(runtime: Runtime, context: ImplementContext) -> Handoff:
    design = context.last(DESIGN)
    if design is None or design.status is not Status.PASSED:
        raise ValueError(f"Issue {context.issue.id} 还没有通过的方案，不能定案")
    plan_hash = str(design.facts["planHash"])
    decided = decision_for(context, plan_hash)
    if decided is not None:
        return _decided(runtime, context, plan_hash, decided)
    reasons = auto_reasons(design.facts, runtime.settings)
    if not reasons and gate_is_auto(GATE, runtime.settings):
        return _handoff(runtime, context, Status.PASSED, "方案在自动确认的条件内，已自动确认",
                        {"planHash": plan_hash, "auto": True, "approvedBy": "auto", "reasons": []})
    if not reasons:
        reasons = ["方案确认关卡配置为人工"]
    _record_once(runtime, context, plan_hash, reasons)
    return _handoff(runtime, context, Status.PENDING, "方案需要用户确认：" + "；".join(reasons),
                    {"planHash": plan_hash, "auto": False, "reasons": reasons,
                     "gate": _gate(context, design.facts, reasons)})


def auto_reasons(facts: Mapping[str, Any], settings: Settings) -> list[str]:
    """不能自动确认的全部原因；为空时可以自动确认。"""
    reasons = [f"需要拍板：{item['question']}" for item in facts.get("userDecisions") or []]
    reasons += [f"标记 {name}：{(facts['flags'][name] or {}).get('reason') or '已命中'}" for name in FLAGS
                if ((facts.get("flags") or {}).get(name) or {}).get("flagged")]
    reasons += [f"改动高风险文件 {item['path']}：{item['change']}" for item in facts.get("protectedTouches") or []]
    if facts.get("migration"):
        reasons.append("含数据库迁移")
    reasons += [f"新增依赖 {item['name']} {item['version']}" for item in facts.get("newDependencies") or []]
    reasons += [f"删除文件 {item['path']}" for item in facts.get("deletions") or []]
    found = facts.get("risk") or {}
    if found.get("high"):
        reasons += [f"高风险：{reason}" for reason in found.get("reasons") or []]
    threshold = settings.get("boundaries.autoApprove")
    estimate = facts.get("estimate") or {}
    if estimate.get("files", 0) > threshold["files"] or estimate.get("lines", 0) > threshold["lines"]:
        reasons.append(f"预估 {estimate.get('files')} 个文件、{estimate.get('lines')} 行，超出自动确认门槛 "
                       f"{threshold['files']} 个文件、{threshold['lines']} 行")
    conflicts = ((facts.get("frontendDesign") or {}).get("planConflicts")) or []
    reasons += [f"前端设计说明与方案冲突：{item}" for item in conflicts]
    return list(dict.fromkeys(reasons))


def decision_for(context: ImplementContext, plan_hash: str) -> Decision | None:
    """停在关卡等确认的同一份方案之后，用户给出的定案决定。"""
    pending = context.last(POINT)
    if pending is None or pending.status is not Status.PENDING or pending.facts.get("planHash") != plan_hash:
        return None
    since = pending.created_at or ""
    return next((item for item in reversed(context.decisions) if item.at > since and item.point == POINT), None)


def confirmed(context: ImplementContext) -> bool:
    """编码前核对：最近一次定案已通过，且所确认的就是当前这份方案。"""
    design, approved = context.last(DESIGN), context.last(POINT)
    return (design is not None and approved is not None and approved.status is Status.PASSED
            and approved.facts.get("planHash") == design.facts.get("planHash"))


def record(runtime: Runtime, issue_id: str, decision: Decision) -> None:
    """命令行 approve、reject 的入口：先持久化(来源、时间、原文)，下次推进时由停着的那一步取用。"""
    if decision.verdict not in (APPROVE, REJECT):
        raise ValueError(f"决定只能是 {APPROVE} 或 {REJECT}：{decision.verdict}")
    if decision.verdict == REJECT and not (decision.note or "").strip():
        raise ValueError("不通过必须写明原因(--note)")
    found = issues.get(runtime.conn, issue_id)
    if found is None:
        raise LookupError(f"没有 Issue {issue_id}")
    extra = dict(found.extra)
    extra[DECISIONS] = [*(extra.get(DECISIONS) or []), asdict(decision)]
    issue_files.write(runtime, replace(found, extra=extra))


def _decided(runtime: Runtime, context: ImplementContext, plan_hash: str, decided: Decision) -> Handoff:
    note = decided.note
    if decided.verdict == REJECT:
        return _handoff(runtime, context, Status.FAILED, f"用户否决了方案：{note or '没有说明'}",
                        {"planHash": plan_hash, "auto": False, "rejected": True, "note": note})
    return _handoff(runtime, context, Status.PASSED, "用户已确认方案",
                    {"planHash": plan_hash, "auto": False, "approvedBy": "user", "note": note,
                     "option": decided.option})


def _gate(context: ImplementContext, facts: Mapping[str, Any], reasons: list[str]) -> dict[str, Any]:
    issue = context.issue.id
    steps = "\n".join(f"{number}. `{item['file']}`：{item['change']}"
                      for number, item in enumerate(facts.get("steps") or [], start=1))
    return {
        "decision": f"确认方案：{facts.get('summary')}\n\n{steps}",
        "options": [f"通过：tightrein approve {issue}",
                    f"不通过，按原因重出方案：tightrein reject {issue} --note \"<原因>\""],
        "recommendation": "看过需要确认的原因后决定；方案本身已通过程序核对",
        "reason": "\n".join(f"- {item}" for item in reasons),
        "ifNot": "Issue 停在定案，不会开始编码",
        "command": f"tightrein approve {issue}",
    }


def _record_once(runtime: Runtime, context: ImplementContext, plan_hash: str, reasons: list[str]) -> None:
    """同一份方案不能自动确认的原因只记一次事件(按幂等键)，重复推进不重复记。"""
    key = f"{context.issue.id}:{POINT}:reasons:{plan_hash[:16]}"

    def emit() -> list[str]:
        runtime.events.emit(run=runtime.run, subject=context.issue.id, point=POINT, kind="decision",
                            summary="方案需要用户确认：" + "；".join(reasons))
        return reasons

    operations.run_once(runtime.conn, key, emit, runtime.clock, subject=context.issue.id, point=POINT)


def _handoff(runtime: Runtime, context: ImplementContext, status: Status, summary: str,
             facts: dict[str, Any]) -> Handoff:
    return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=status, summary=summary,
                   facts=facts, metrics=Metrics(calls=0))
