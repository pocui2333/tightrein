"""方案(implement.design)的提示变量。

其他未完成的 Issue 列给方案：本 Issue 要用到的代码要由其中某几个先合入时，方案据此写出前置 Issue(blockedBy)，不问用户。
Issue 正文多用 GitHub 编号互相引用(#25)，列表带上镜像的 GitHub 编号，方案才对得上本地编号。

回归或重开后再修(assess/issue/attempts，第 2 次及以后)：「这一次修复」写明上一次修复的结果(合并了没有、PR 与合并
提交)与上一次交付之后的变化(回归、重开、追加的问题，取自历史)。正文从「结论」到「引用」是最初的问题，上一次已按它
修过，方案据此只处理这些变化，不把上一次的修复重做一遍。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from tightrein.assess.issue import attempts, blockers
from tightrein.assess.issue.body import history_line
from tightrein.assess.issue.github import mirror_number
from tightrein.assess.issue.transitions import HISTORY
from tightrein.implement.prompts.common import (
    NONE,
    acceptance,
    decisions_text,
    designs_text,
    issue_text,
    knowledge_text,
    list_text,
    notes_text,
)
from tightrein.protocol.security import external
from tightrein.store.tables import issues

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext, Risk
    from tightrein.protocol.runtime import Runtime
    from tightrein.store.tables.issues import Issue

DESIGN_ACCEPTED = "用户已同意按设计层面的根因修复：设计问题照常在 flags.design 中标出，但不作为中止理由，照常给出完整方案。"
# 上一次交付之后的这些事件是这一次修复要处理的变化
CHANGES = ("regress", "reopen", "append")
REASONS = {"regress": "回归", "reopen": "重开"}


def variables(runtime: Runtime, context: ImplementContext, *, risk: Risk, replan: Sequence[str],
              feedback: Sequence[str], design_accepted: bool) -> dict[str, str]:
    return {
        "issue": issue_text(context),
        "attempt": attempt_text(context.issue, runtime.language),
        "acceptance": list_text(acceptance(context.body)),
        "notes": notes_text(context),
        "risk": _risk_text(risk),
        "limits": _limits_text(runtime),
        "replan": list_text(replan),
        "decisions": "\n".join(part for part in (DESIGN_ACCEPTED if design_accepted else "",
                                                  decisions_text(context.decisions)) if part),
        "knowledge": knowledge_text(runtime, context),
        "feedback": list_text(feedback),
        "open_issues": open_issues_text(runtime, context),
        "designs": designs_text(runtime),
    }


def attempt_text(issue: Issue, language: str) -> str:
    """回归或重开后再修时：上一次修复的结果，与它交付之后的回归、重开、追加的问题(这一次要处理的)；第 1 次为「无」。
    上一次交付之前追加的问题，上一次的方案与审查已在正文中看到过，不算变化。"""
    done = list(issue.extra.get(attempts.ATTEMPTS) or [])
    if not done:
        return NONE
    last = done[-1]
    history = [entry for entry in issue.extra.get(HISTORY) or [] if isinstance(entry, dict)]
    ended = str(last.get("endedAt") or "")
    delivered = [str(entry["at"]) for entry in history if entry.get("event") == "deliver" and str(entry["at"]) <= ended]
    since = max(delivered, default=ended)
    changes = [history_line(entry, language) for entry in history
               if entry.get("event") in CHANGES and str(entry["at"]) >= since]
    reason = REASONS.get(str(last.get("reason")), str(last.get("reason") or ""))
    head = f"第 {attempts.current(issue)} 次修复。上一次(第 {last.get('attempt')} 次){_outcome(last)}，之后因{reason}重新打开。"
    return "\n".join([head, "上一次交付之后的变化(这一次要处理的就是这些；追加的问题的内容在正文「追加的问题」中该问题编号之下)：",
                      *(changes or ["- 历史中没有记下"])])


def _outcome(last: Mapping[str, Any]) -> str:
    """上一次修复的结果：合并了没有，PR 与合并提交。"""
    merged = f"合并提交 {str(last['mergeCommit'])[:12]}" if last.get("mergeCommit") else None
    found = "，".join(item for item in (f"PR #{last['pr']}" if last.get("pr") else None, merged) if item)
    return ("的修复已合并" if merged else "的修复没有合并") + (f"({found})" if found else "")


def open_issues_text(runtime: Runtime, context: ImplementContext) -> str:
    """除本 Issue 外还没合入主干的 Issue：本地编号、GitHub 编号(有镜像时)、状态与标题。标题可能来自外部，整体包住。"""
    lines = [f"- {item.id}{_github(item)}，{item.status}：{item.title}" for item in issues.find(runtime.conn)
             if item.id != context.issue.id and item.status not in blockers.SETTLED]
    return external("open issues", "\n".join(lines)) if lines else NONE


def _github(issue: Issue) -> str:
    number = mirror_number(issue)
    return f"(GitHub #{number})" if number is not None else ""


def _risk_text(risk: Risk) -> str:
    if not risk.high:
        return "常规：没有命中风险规则。"
    return "高风险，逐项说明怎样处理：\n" + list_text(risk.reasons)


def _limits_text(runtime: Runtime) -> str:
    settings = runtime.settings
    cap = settings.get("boundaries.changeCap")
    return "\n".join([
        f"- 单个 PR 改动不超过 {cap['files']} 个文件、{cap['lines']} 行(不含测试、锁文件与生成文件)，是硬上限",
        f"- 禁改文件(不能出现在方案里)：{'、'.join(settings.get('boundaries.protected.forbidden'))}",
        f"- 高风险文件(要改就写进 protectedTouches)：{'、'.join(settings.get('boundaries.protected.highRisk'))}",
    ])
