"""自动合并的条件与合并。

全部满足才合并，否则记下原因(与上次相同时不再记，免得每次跟踪都刷一遍)：
1. 最后一轮检查全部通过，且没有用户接受的未通过项；
2. PR 打开、不是草稿；由本工具合并时还要 MERGEABLE 且 mergeStateStatus 为 CLEAN 或 HAS_HOOKS；
3. 评审只看每个评审者按顺序的最后一次「批准、请求修改、撤销」：最后一次为请求修改的才阻断；
4. PR 头部等于本工具最近一次推送的 commit(PR 上没有别人的提交)；
5. CI 通过(或没有检查)；
6. 改动没有命中高风险路径(protocol/boundaries 的高风险级：CI、依赖、迁移、权限认证、密钥配置)。命中时一律不自动合并，
   写待决定文档交人，同一组命中只写一次；关卡 merge 配成人工时同样交人。

合并用 `gh pr merge --match-head-commit <判断时的 head> --delete-branch`：判断后有人再推送就合并失败。主分支要求必需
检查、或仓库开了 GitHub 自带的合并队列时，只按不看 CI 的条件判断后开启 GitHub 原生自动合并(`--auto`)，同一 head 只
开一次，由 GitHub 等检查通过后合并或排进它的队列。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tightrein.protocol import boundaries
from tightrein.protocol.git import CommandFailed, GitHub
from tightrein.protocol.git.github import MergeFacts
from tightrein.protocol.runtime import Runtime
from tightrein.release import ci
from tightrein.release.ci import CiState
from tightrein.release.record import POINT_MERGE, Delivery, ReleaseState, emit

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
MERGE_QUEUE_RULE = "merge_queue"
FORBIDDEN = "HTTP 403"  # 免费账号的私有仓库没有规则集功能：视为没有
# 一次运行内各仓库的「是否要求必需检查」「是否交给 GitHub」只查一次
_CACHE: dict[tuple[str, str, str], bool] = {}


@dataclass
class MergeDecision:
    merged: bool = False
    native: bool = False  # 已开启(或之前已开启)GitHub 原生自动合并
    reasons: list[str] = field(default_factory=list)
    gate: str | None = None  # 需要人决定时的关卡：high_risk_merge 或 merge
    gate_reason: str | None = None
    new_gate: bool = False  # 这组原因是第一次出现(要写待决定文档)


def changes_requested(reviews: Sequence[Mapping[str, Any]]) -> list[str]:
    """最近一次评审为「请求修改」的评审者(只看批准、请求修改、撤销三种结论，按提交顺序取最后一次)。"""
    latest: dict[str, str] = {}
    for review in reviews:
        state = review.get("state")
        if state in REVIEW_STATES:
            latest[(review.get("author") or {}).get("login") or "?"] = state
    return sorted(login for login, state in latest.items() if state == CHANGES_REQUESTED)


def own_blockers(delivery: Delivery, state: ReleaseState, facts: MergeFacts) -> list[str]:
    """不看 GitHub 检查状态的条件：开启 GitHub 原生自动合并前也要满足。"""
    reasons = []
    if not delivery.checks_passed or delivery.accepted:
        reasons.append("最后一轮检查没有全部通过，或接受了未通过项")
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
           github: GitHub) -> MergeDecision:
    """判断并在条件满足时合并(或开启 GitHub 原生自动合并)；state 中的原因、已开启的 head、已交人的原因随之更新。"""
    decision = MergeDecision()
    risky = risky_paths(runtime, delivery)
    if risky:
        return _to_human(runtime, issue, state, decision, HIGH_RISK_GATE,
                         "改动涉及高风险路径，不自动合并：" + "、".join(risky))
    if not boundaries.gate_is_auto(MERGE_GATE, runtime.settings):
        return _to_human(runtime, issue, state, decision, MERGE_GATE, "本项目的合并关卡为人工")
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
    if decision.native:
        state.native_head = facts.head
        emit(runtime, issue, POINT_MERGE, "action", f"已为 PR #{number} 开启 GitHub 自动合并({method})")
    else:
        decision.merged = True
        emit(runtime, issue, POINT_MERGE, "action", f"已合并 PR #{number}({method})并删除远程分支")
    return decision


def _unmerged(runtime: Runtime, issue: str, state: ReleaseState, decision: MergeDecision,
              reasons: list[str]) -> MergeDecision:
    decision.reasons = reasons
    if state.merge_reasons != reasons:
        emit(runtime, issue, POINT_MERGE, "decision", "未自动合并：" + "；".join(reasons))
        state.merge_reasons = reasons
    return decision


def _to_human(runtime: Runtime, issue: str, state: ReleaseState, decision: MergeDecision, gate: str,
              reason: str) -> MergeDecision:
    decision.gate, decision.gate_reason = gate, reason
    signature = [gate, reason]
    decision.new_gate = state.decision != signature
    state.decision = signature
    return _unmerged(runtime, issue, state, decision, [reason])
