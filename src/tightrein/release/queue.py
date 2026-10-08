"""合并队列：多个 PR 待合并时依次合并，不用每个等下一轮。

- 排队：发布中的 Issue 按严重度(P0 在前，没有评级的最后)、再按放行时间排；手动接管的不碰；
- 处理队首：release 依次做同步 main、推送、等 CI、判断合并；合并后接着处理下一个；
- 后面的 PR：release 先把刚合并后的 main 合并进来，改到与 main 新提交相同的文件时退回实施重新审查(sync.py)，
  否则等 CI，满足条件即合并；
- 出问题的跳过：同步冲突、检查失败的由 release 转为待决定(写明原因)，移出队列，继续处理后面的；
- 一次运行中循环处理，直到队列空、或遇到需要用户的项(高风险合并、人工合并关卡)、或到达时间上限
  (controls."release.queue".timeLimit)；
- 仓库开了 GitHub 自带的合并队列(或主分支要求必需检查)时直接交给它：release 对每个 PR 只做到开启自动合并
  (进它的队列)就返回，不在这里等 CI，也就不按这里的顺序串行。
"""

from __future__ import annotations

from datetime import timedelta

from tightrein.assess.issue.transitions import IssueStatus
from tightrein.implement.implement import StepOutcome
from tightrein.protocol.handoff import Status
from tightrein.protocol.naming import parse_duration
from tightrein.protocol.runtime import Runtime
from tightrein.release.record import load_state
from tightrein.release.release import release
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

POINT = "release.queue"
SEVERITIES = ("P0", "P1", "P2", "P3")
APPROVED_AT = "approvedAt"  # 评估放行时写在 Issue extra 中的时间；没有时取第一次进入发布的时间


def order(found: list[Issue]) -> list[Issue]:
    """按严重度、再按放行时间排队；手动接管的不排。"""
    queued = [issue for issue in found if issue.held_by is None]
    return sorted(queued, key=lambda issue: (_severity(issue), _approved_at(issue), issue.id))


def queue(runtime: Runtime) -> list[StepOutcome]:
    pending = order(issues.find(runtime.conn, status=IssueStatus.RELEASING))
    if not pending:
        return []
    limit = parse_duration(str(runtime.settings.control(POINT, "timeLimit")))
    deadline = runtime.clock.now() + timedelta(seconds=limit)
    outcomes: list[StepOutcome] = []
    for issue in pending:
        if runtime.clock.now() >= deadline:
            break
        outcome = release(runtime, issue.id)
        outcomes.append(outcome)
        if _needs_user(runtime, issue.id, outcome):
            break
    return outcomes


def _needs_user(runtime: Runtime, issue: str, outcome: StepOutcome) -> bool:
    """停在人工合并关卡(仍在发布中)；转为待决定的已移出队列，不挡后面的。"""
    current = issues.get(runtime.conn, issue)
    return outcome.status == Status.PENDING and current is not None and current.status == IssueStatus.RELEASING


def _severity(issue: Issue) -> int:
    return SEVERITIES.index(issue.severity) if issue.severity in SEVERITIES else len(SEVERITIES)


def _approved_at(issue: Issue) -> str:
    return str(issue.extra.get(APPROVED_AT) or load_state(issue).entered_at or "")
