"""自动合并的条件(design 7.6，redesign/07-release.md 第 2 节、06-verify.md 第 3 节，关卡 gates.merge)：全部满足才合并，返回不满足的原因
(空表示可以合并)。改动命中 release.autoMergeBlockPaths 时一律不自动合并(blocked_paths，由调用方写决策简报)；仓库有
分支保护与必需检查时只按 own_blockers 判断后开启 GitHub 原生自动合并，由平台等检查通过后合并。

1. 合并前验证结论为通过；
2. 修复最后一轮确定性检查全部通过，且没有以 --accept-findings 接受的未通过项(评审没有未处理的阻断意见)；
3. PR 打开、不是草稿、mergeable 为 MERGEABLE、mergeStateStatus 为 CLEAN 或 HAS_HOOKS(无冲突，必需检查与必需评审都已
   满足，且没有未通过或进行中的检查)；
4. 没有评审者最近一次评审为 CHANGES_REQUESTED；
5. PR 头部 commit 等于本工具最近一次推送的 commit(PR 上没有本工具之外的提交)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.guards.protected import matching_pattern
from tightrein.vcs.parse import MergeFacts

PASSED = "passed"
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


def changes_requested(reviews: Sequence[Mapping[str, Any]]) -> list[str]:
    """最近一次评审为「请求修改」的评审者(只看批准、请求修改、撤销三种结论，按提交顺序取最后一次)。"""
    latest: dict[str, str] = {}
    for review in reviews:
        state = review.get("state")
        if state in REVIEW_STATES:
            latest[(review.get("author") or {}).get("login") or "?"] = state
    return sorted(login for login, state in latest.items() if state == CHANGES_REQUESTED)


def blocked_paths(config: ProjectConfig, changed: Sequence[str]) -> list[tuple[str, str]]:
    """改动中命中 release.autoMergeBlockPaths 的文件与规则(CI 工作流、依赖清单、迁移、权限认证、密钥配置等)。"""
    patterns = tuple(config.get("release.autoMergeBlockPaths") or ())
    found = [(path, matching_pattern(path, patterns)) for path in changed]
    return [(path, pattern) for path, pattern in found if pattern is not None]


def own_blockers(local: Mapping[str, Any] | None, fix: Mapping[str, Any] | None, release: Mapping[str, Any],
                 facts: MergeFacts) -> list[str]:
    """不看 GitHub 检查状态的条件：开启 GitHub 原生自动合并前也要满足。"""
    reasons = []
    if local is None or local.get("conclusion") != PASSED:
        reasons.append("合并前验证的结论不是通过")
    rounds = (fix or {}).get("rounds") or []
    last = rounds[-1] if rounds else None
    if last is None or not last["checksPassed"] or last["failures"] or release.get("acceptedFindings"):
        reasons.append("修复评审有未处理的阻断意见(最后一轮检查未全部通过或接受了未通过项)")
    if facts.state != OPEN:
        reasons.append(f"PR 的状态为 {facts.state}")
    if facts.draft:
        reasons.append("PR 是草稿")
    requested = changes_requested(facts.reviews)
    if requested:
        reasons.append(f"{'、'.join(requested)} 请求修改")
    pushed = ((release.get("push") or {}).get("commits") or [None])[-1]
    if pushed is None or facts.head != pushed:
        reasons.append("PR 头部 commit 不是本工具最近一次推送的 commit")
    return reasons


def blockers(local: Mapping[str, Any] | None, fix: Mapping[str, Any] | None, release: Mapping[str, Any],
             facts: MergeFacts) -> list[str]:
    """由 tightrein 合并时的全部条件：own_blockers 加上无冲突、必需检查与必需评审都已满足。"""
    reasons = own_blockers(local, fix, release, facts)
    if facts.mergeable != MERGEABLE:
        reasons.append("PR 与主分支冲突" if facts.mergeable == "CONFLICTING" else "GitHub 尚未算出能否合并")
    if facts.merge_state not in CLEAN_STATES:
        reasons.append(MERGE_STATE_REASONS.get(facts.merge_state or "UNKNOWN", f"合并状态为 {facts.merge_state}"))
    return list(dict.fromkeys(reasons))
