"""方案的提示变量：回归或重开后再修时，写明上一次修复的结果与它交付之后的变化(回归、重开、追加的问题)。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from tightrein.implement.context import Risk
from tightrein.implement.design.design import SCHEMA
from tightrein.implement.prompts import design
from tightrein.prompts.build import build
from tightrein.protocol.handoff import load_schema

MERGE = "5f20e056e39d19710ced04ff2f67208392614842"


def _entry(at: str, event: str, note: str | None, actor: str = "tightrein") -> dict[str, Any]:
    return {"at": at, "event": event, "actor": actor, "reason": None, "note": note}


HISTORY = [
    _entry("2026-10-14T13:00:00Z", "append", "追加问题 P-0005"),  # 上一次交付之前：上一次已看到
    _entry("2026-10-14T13:16:42Z", "deliver", "交给发布"),
    _entry("2026-10-14T13:40:00Z", "append", "追加问题 P-0059"),  # 交付之后追加：上一次没处理
    _entry("2026-10-14T13:56:14Z", "merge", "PR #59 已合并", actor="release"),
    _entry("2026-10-14T14:35:49Z", "regress", "关联问题 P-0008 回归", actor="collect.dedup"),
    _entry("2026-10-14T14:42:20Z", "append", "追加问题 P-0060，严重度由 P3 提升为 P2"),
    _entry("2026-10-14T15:36:00Z", "start", None),
]


def test_a_reopened_issue_tells_the_design_what_changed_since_the_last_fix(world: Any) -> None:
    context = world.context()
    variables = design.variables(world.runtime, context, risk=Risk(False, ()), replan=[], feedback=[],
                                 design_accepted=False)
    assert variables["attempt"] == "无"  # 第 1 次修复
    last = {"attempt": 1, "endedAt": "2026-10-14T14:35:49Z", "reason": "regress", "branch": "bugfix-4-is-quota",
            "pr": 59, "mergeCommit": MERGE, "deploy": MERGE, "release": None}
    context.issue = replace(context.issue, extra={**context.issue.extra, "attempt": 2, "attempts": [last],
                                                  "history": HISTORY})
    variables = design.variables(world.runtime, context, risk=Risk(False, ()), replan=[], feedback=[],
                                 design_accepted=False)
    assert variables["attempt"] == (
        "第 2 次修复。上一次(第 1 次)的修复已合并(PR #59，合并提交 5f20e056e39d)，之后因回归重新打开。\n"
        "上一次的修复在主干上：只处理下面的变化，不把上一次的修复重做一遍。\n"
        "上一次交付之后的变化(追加的问题的内容在正文「追加的问题」中该问题编号之下)：\n"
        "- 2026-10-14T13:40:00Z append(操作者 tightrein)：追加问题 P-0059\n"
        "- 2026-10-14T14:35:49Z regress(操作者 collect.dedup)：关联问题 P-0008 回归\n"
        "- 2026-10-14T14:42:20Z append(操作者 tightrein)：追加问题 P-0060，严重度由 P3 提升为 P2")
    text = build("implement.design", variables, language="zh", schema=load_schema(SCHEMA), tool="claude").text
    assert "## 这一次修复(回归或重开后再修时)\n\n第 2 次修复。" in text
    assert "不把上一次的修复重做一遍" in text


def test_an_unmerged_previous_attempt_means_the_whole_body_is_still_to_fix(world: Any) -> None:
    """上一次的 PR 关掉没合并、后来在 GitHub 上重开：主干上没有上一次的修复，不能只处理变化(变化里只有一次重开)。"""
    context = world.context()
    last = {"attempt": 1, "endedAt": "2026-10-14T14:00:00Z", "reason": "reopen", "pr": 12, "mergeCommit": None}
    context.issue = replace(context.issue, extra={
        **context.issue.extra, "attempt": 2, "attempts": [last],
        "history": [_entry("2026-10-14T14:00:00Z", "reopen", "在 GitHub 上重新打开", actor="github")]})
    variables = design.variables(world.runtime, context, risk=Risk(False, ()), replan=[], feedback=[],
                                 design_accepted=False)
    assert variables["attempt"].splitlines() == [
        "第 2 次修复。上一次(第 1 次)的修复没有合并(PR #12)，之后因重开重新打开。",
        "主干上没有上一次的修复：正文的问题都还没修，照正文全部处理(含「追加的问题」)，下面的变化一并处理。",
        "上一次交付之后的变化(追加的问题的内容在正文「追加的问题」中该问题编号之下)：",
        "- 2026-10-14T14:00:00Z reopen(操作者 github)：在 GitHub 上重新打开"]
    text = build("implement.design", variables, language="zh", schema=load_schema(SCHEMA), tool="claude").text
    assert "上一次的修复没有合并或已被撤销时，worktree 中没有它，正文的问题都还没修，照正文全部处理。" in text
    assert "正文从「结论」到「引用」写的是最初的问题，上一次已经按它修过" not in text  # 不再不分情况地这样说


def test_a_regression_with_a_revert_pr_asks_the_design_to_check_the_worktree(world: Any) -> None:
    """验收中回归时提了撤销 PR：合并了的话上一次的修复已不在主干上，由方案看代码判断。"""
    issue = world.context().issue
    last = {"attempt": 1, "endedAt": "2026-10-14T14:35:49Z", "reason": "regress", "pr": 59, "mergeCommit": MERGE,
            "release": {"revert": {"branch": "hotfix-4-revert-quota", "pr": 61, "url": "https://x/pull/61"}}}
    regressed = replace(issue, extra={**issue.extra, "attempt": 2, "attempts": [last], "history": HISTORY})
    lines = design.attempt_text(regressed, "zh").splitlines()
    assert lines[0] == "第 2 次修复。上一次(第 1 次)的修复已合并(PR #59，合并提交 5f20e056e39d)，验收中回归后提了撤销 PR #61，" \
                       "之后因回归重新打开。"
    assert lines[1].startswith("撤销 PR 合并了的话主干上已没有上一次的修复：先看修复 worktree 中根因位置的代码")
    assert "不把上一次的修复重做一遍" not in lines[1]
