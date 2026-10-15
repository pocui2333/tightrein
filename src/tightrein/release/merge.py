"""自动合并的条件与合并。

全部满足才合并，否则记下原因(与上次相同时不再记，免得每次跟踪都刷一遍)：
1. 最后一轮检查全部通过，且没有用户接受的未通过项；
2. PR 打开、不是草稿；由本工具合并时还要 MERGEABLE 且 mergeStateStatus 为 CLEAN 或 HAS_HOOKS；
3. 评审只看每个评审者按顺序的最后一次「批准、请求修改、撤销」：最后一次为请求修改的才阻断；
4. PR 头部等于本工具最近一次推送的 commit(PR 上没有别人的提交)；
5. CI 通过(或没有检查)；
6. 改动没有命中高风险路径(protocol/boundaries 的高风险级：CI、依赖、迁移、权限认证、密钥配置)，或命中的每一个都已在
   定案时由用户确认：最近一次定案由用户通过、确认的就是最近一次方案，且方案的 protectedTouches 列了这个路径(定案关卡
   逐条列给用户)。有没确认过的就交人(high_risk_merge)，写待决定文档，同一组命中、同一份交付只写一次；关卡 merge 配成
   人工时同样交人。

交人之后用户 `tightrein approve` 通过这一关(关卡出现之后的最近一次决定)，本工具照常按 1 到 5 判断后合并，与自动合并
同一方式；选「自己在 GitHub 上合并」等其他选项的不合并，只等 PR 在 GitHub 上合并或关闭。关卡上的通过不改变 1：交付带
用户接受的未通过项(或最后一轮检查没有全部通过)时本工具不合并，交人时记下这个原因(manual)，待决定文档直接请人在
GitHub 上合并，不提通过后由本工具合并。合并用到的确认(定案或关卡上的)记进合并的交接(`approvals`)。

合并用 `gh pr merge --match-head-commit <判断时的 head> --delete-branch`：判断后有人再推送就合并失败。主分支要求必需
检查、或仓库开了 GitHub 自带的合并队列时，只按不看 CI 的条件判断后开启 GitHub 原生自动合并(`--auto`)，同一 head 只
开一次，由 GitHub 等检查通过后合并或排进它的队列。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tightrein.implement.approve import approve
from tightrein.protocol import boundaries
from tightrein.protocol.git import CommandFailed, GitHub
from tightrein.protocol.git.github import MergeFacts
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.recovery import checkpoints
from tightrein.protocol.runtime import Runtime
from tightrein.release import ci
from tightrein.release.ci import CiState
from tightrein.release.record import POINT_MERGE, Delivery, ReleaseState, emit, now_iso

OPEN = "OPEN"
MERGEABLE = "MERGEABLE"
CLEAN_STATES = frozenset({"CLEAN", "HAS_HOOKS"})
CHANGES_REQUESTED = "CHANGES_REQUESTED"
REVIEW_STATES = frozenset({"APPROVED", CHANGES_REQUESTED, "DISMISSED"})
MERGE_STATE_REASONS = {
    "BLOCKED": "必需检查或必需评审尚未满足",
    "BEHIND": "修复分支落后于主分支",
    "DIRTY": "与主分支冲突",
    "UNSTABLE": "有未通过或进行中的检查",
    "DRAFT": "PR 是草稿",
    "UNKNOWN": "GitHub 尚未算出能否合并",
}
HIGH_RISK_GATE = "high_risk_merge"
MERGE_GATE = "merge"
APPROVE = "approve"
BY_TOOL = 1  # 合并关卡待决定文档的选项(release.py 的 _to_gate)：第 1 个由本工具合并，没选时同样
SELF_MERGE = 2  # 第 2 个：用户自己在 GitHub 上合并，本工具不合并
CHECKS_NOT_PASSED = "最后一轮检查没有全部通过，或接受了未通过项"
LEFT_TO_GITHUB = "本工具不合并，等 PR 在 GitHub 上合并或关闭"
MERGE_QUEUE_RULE = "merge_queue"
FORBIDDEN = "HTTP 403"  # 免费账号的私有仓库没有规则集功能：视为没有
# 一次运行内各仓库的「是否要求必需检查」「是否交给 GitHub」只查一次
_CACHE: dict[tuple[str, str, str], bool] = {}


@dataclass(frozen=True)
class Approval:
    """用户对这次合并(或其中的高风险改动)的确认：在哪一关、何时、原话；定案时的确认另有方案哈希与确认过的路径。"""

    point: str  # implement.approve(定案)、high_risk_merge 或 merge(合并关卡)
    at: str
    note: str | None = None
    option: int | None = None
    plan_hash: str | None = None
    paths: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"point": self.point, "at": self.at, "note": self.note, "option": self.option,
                "planHash": self.plan_hash, "paths": list(self.paths)}


@dataclass
class MergeDecision:
    merged: bool = False
    native: bool = False  # 已开启(或之前已开启)GitHub 原生自动合并
    reasons: list[str] = field(default_factory=list)
    gate: str | None = None  # 需要人决定时的关卡：high_risk_merge 或 merge
    gate_reason: str | None = None
    new_gate: bool = False  # 这组原因是第一次出现(要写待决定文档)
    manual: list[str] = field(default_factory=list)  # 交人时关卡上通过也不由本工具合并的原因(交付本身决定)
    approvals: list[Approval] = field(default_factory=list)  # 放行高风险路径、合并关卡的用户确认


def changes_requested(reviews: Sequence[Mapping[str, Any]]) -> list[str]:
    """最近一次评审为「请求修改」的评审者(只看批准、请求修改、撤销三种结论，按提交顺序取最后一次)。"""
    latest: dict[str, str] = {}
    for review in reviews:
        state = review.get("state")
        if state in REVIEW_STATES:
            latest[(review.get("author") or {}).get("login") or "?"] = state
    return sorted(login for login, state in latest.items() if state == CHANGES_REQUESTED)


def delivery_blockers(delivery: Delivery) -> list[str]:
    """交付本身使本工具不合并的原因：换一份交付才会变，合并关卡上的通过也不改变，只能由人在 GitHub 上合并。"""
    return [CHECKS_NOT_PASSED] if not delivery.checks_passed or delivery.accepted else []


def own_blockers(delivery: Delivery, state: ReleaseState, facts: MergeFacts) -> list[str]:
    """不看 GitHub 检查状态的条件：开启 GitHub 原生自动合并前也要满足。"""
    reasons = delivery_blockers(delivery)
    if facts.state != OPEN:
        reasons.append(f"PR 的状态为 {facts.state}")
    if facts.draft:
        reasons.append("PR 是草稿")
    requested = changes_requested(facts.reviews)
    if requested:
        reasons.append(f"{'、'.join(requested)} 请求修改")
    if state.pushed is None or facts.head != state.pushed:
        reasons.append("PR 头部 commit 不是本工具最近一次推送的 commit")
    return reasons


def blockers(delivery: Delivery, state: ReleaseState, facts: MergeFacts, checks: CiState) -> list[str]:
    """由本工具合并时的全部条件。"""
    reasons = own_blockers(delivery, state, facts)
    if facts.mergeable != MERGEABLE:
        reasons.append("PR 与主分支冲突" if facts.mergeable == "CONFLICTING" else "GitHub 尚未算出能否合并")
    if facts.merge_state not in CLEAN_STATES:
        reasons.append(MERGE_STATE_REASONS.get(facts.merge_state or "UNKNOWN", f"合并状态为 {facts.merge_state}"))
    if checks.reason is not None:
        reasons.append(checks.reason)
    return list(dict.fromkeys(reasons))


def risky_paths(runtime: Runtime, delivery: Delivery) -> list[str]:
    return sorted({*boundaries.high_risk(delivery.changed_files, runtime.settings), *delivery.high_risk_paths})


def design_approval(runtime: Runtime, issue: str) -> Approval | None:
    """定案时用户确认过的受保护文件：最近一次定案由用户通过(不是自动确认)、确认的就是最近一次方案时，取这份方案
    protectedTouches 的路径(定案关卡把它们逐条列为要确认的原因，见 implement/approve 的 mandatory_reasons)。"""
    latest: dict[str, Handoff] = {}
    for item in checkpoints(runtime.workspace, issue):
        if item.handoff.point in (approve.DESIGN, approve.POINT):
            latest[item.handoff.point] = item.handoff
    design, approved = latest.get(approve.DESIGN), latest.get(approve.POINT)
    if design is None or approved is None or approved.status is not Status.PASSED:
        return None
    plan_hash = design.facts.get("planHash")
    if approved.facts.get("approvedBy") != approve.BY_USER or approved.facts.get("planHash") != plan_hash:
        return None
    paths = approve.protected_paths(design.facts)
    if not paths:
        return None
    return Approval(approve.POINT, approved.created_at or "", approved.facts.get("note"), plan_hash=str(plan_hash),
                    paths=paths)


def gate_approval(decisions: Sequence[Mapping[str, Any]], gate: str, since: str | None) -> Approval | None:
    """关卡出现(since)之后用户在这一关的最近一次决定为通过时的确认；没有、或最近一次为不通过时为 None。"""
    if since is None:
        return None
    found = [item for item in decisions if item.get("point") == gate and str(item.get("at") or "") >= since]
    if not found or found[-1].get("verdict") != APPROVE:
        return None
    latest = found[-1]
    return Approval(gate, str(latest["at"]), latest.get("note"), latest.get("option"))


def requires_checks(runtime: Runtime, github: GitHub) -> bool:
    """主分支有分支保护或规则集要求必需检查。"""
    key = (runtime.run, github.slug, "required")
    if key not in _CACHE:
        _CACHE[key] = github.required_checks(runtime.git.main_branch)
    return _CACHE[key]


def native(runtime: Runtime, github: GitHub) -> bool:
    """主分支要求必需检查，或开了 GitHub 自带的合并队列：交给 GitHub 合并。"""
    key = (runtime.run, github.slug, "native")
    if key not in _CACHE:
        _CACHE[key] = requires_checks(runtime, github) or merge_queue(runtime, github)
    return _CACHE[key]


def merge_queue(runtime: Runtime, github: GitHub) -> bool:
    """主分支的规则集里有合并队列。"""
    try:
        rules = github.query_json("api", f"repos/{github.slug}/rules/branches/{runtime.git.main_branch}",
                           repo=False)
    except CommandFailed as error:
        if FORBIDDEN in error.stderr:
            return False
        raise
    return any(rule.get("type") == MERGE_QUEUE_RULE for rule in rules or [])


def decide(runtime: Runtime, issue: str, number: int, delivery: Delivery, state: ReleaseState, checks: CiState,
           github: GitHub, decisions: Sequence[Mapping[str, Any]] = ()) -> MergeDecision:
    """判断并在条件满足时合并(或开启 GitHub 原生自动合并)；state 中的原因、已开启的 head、已交人的原因随之更新。

    decisions 为 Issue 上用户的决定(issues.extra.decisions)：认出用户在合并关卡上的通过。"""
    decision = MergeDecision()
    gate, reason = _gate(runtime, issue, delivery, decision)
    if gate is not None:
        # 同一关卡、同一组原因、同一份交付才沿用关卡上的通过：重新交付后改动变了，要重新确认
        signature = [gate, reason, delivery.diff_hash]
        approved = gate_approval(decisions, gate, state.gate_at) if state.decision == signature else None
        manual = delivery_blockers(delivery)
        if approved is None:
            decision.manual = manual
            return _to_human(runtime, issue, state, decision, gate, reason, signature)
        decision.approvals.append(approved)
        if approved.option not in (None, BY_TOOL):
            return _unmerged(runtime, issue, state, decision, [
                f"用户在合并关卡选了第 {approved.option} 个选项，{LEFT_TO_GITHUB}"])
        if manual:
            return _unmerged(runtime, issue, state, decision, [*manual, f"关卡上的通过不改变这一点，{LEFT_TO_GITHUB}"])
    facts = github.merge_facts(number)
    decision.native = native(runtime, github)
    if decision.native:
        reasons = own_blockers(delivery, state, facts)
        if checks.state == ci.FAILED and checks.reason is not None:
            reasons.append(checks.reason)
        if not reasons and state.native_head == facts.head:
            return decision
    else:
        reasons = blockers(delivery, state, facts, checks)
    if reasons:
        decision.native = False
        return _unmerged(runtime, issue, state, decision, reasons)
    method = str(runtime.settings.control("release", "mergeMethod"))
    github.merge_pr(number, facts.head or "", method, auto=decision.native,
                    scope=runtime.scope(issue, POINT_MERGE))
    state.merge_reasons = []
    covered = "".join(f"；{_approval_text(item)}" for item in decision.approvals)
    if decision.native:
        state.native_head = facts.head
        emit(runtime, issue, POINT_MERGE, "action", f"已为 PR #{number} 开启 GitHub 自动合并({method}){covered}")
    else:
        decision.merged = True
        emit(runtime, issue, POINT_MERGE, "action", f"已合并 PR #{number}({method})并删除远程分支{covered}")
    return decision


def _gate(runtime: Runtime, issue: str, delivery: Delivery, decision: MergeDecision) -> tuple[str | None, str]:
    """要人决定的关卡与原因，不要时为 (None, "")；高风险路径全部在定案时由用户确认过的不为它停下，那次确认记进 decision。"""
    risky = risky_paths(runtime, delivery)
    if risky:
        confirmed = design_approval(runtime, issue)
        known = [path for path in risky if confirmed is not None and path in confirmed.paths]
        unknown = [path for path in risky if path not in known]
        if unknown:
            return HIGH_RISK_GATE, "改动涉及高风险路径，不自动合并：" + "、".join(unknown) + (
                f"(定案时只确认了 {'、'.join(known)})" if known else "")
        if confirmed is not None:
            decision.approvals.append(confirmed)
    if not boundaries.gate_is_auto(MERGE_GATE, runtime.settings):
        return MERGE_GATE, "本项目的合并关卡为人工"
    return None, ""


def _approval_text(approval: Approval) -> str:
    if approval.point == approve.POINT:
        return f"高风险路径 {'、'.join(approval.paths)} 已在定案时由用户确认({approval.at})"
    return f"用户已在 {approval.point} 关卡通过({approval.at})"


def _unmerged(runtime: Runtime, issue: str, state: ReleaseState, decision: MergeDecision,
              reasons: list[str]) -> MergeDecision:
    decision.reasons = reasons
    if state.merge_reasons != reasons:
        emit(runtime, issue, POINT_MERGE, "decision", "未自动合并：" + "；".join(reasons))
        state.merge_reasons = reasons
    return decision


def _to_human(runtime: Runtime, issue: str, state: ReleaseState, decision: MergeDecision, gate: str,
              reason: str, signature: list[str]) -> MergeDecision:
    decision.gate, decision.gate_reason = gate, reason
    decision.new_gate = state.decision != signature
    if decision.new_gate:
        state.decision, state.gate_at = signature, now_iso(runtime)
    return _unmerged(runtime, issue, state, decision, [reason])
