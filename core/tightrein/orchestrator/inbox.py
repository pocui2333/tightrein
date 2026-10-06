"""待用户决定的收件箱(redesign/09-loop.md 第 4 节)：所有需要用户处理的事项集中在这里，每件附推荐做法。

事项来自状态表与各模块的产出：待确认操作(含撤销 PR)、待确认的修复计划、人工队列、待决定的 Issue(待放行、转人工与熔断)、
交互修复、待合并的 PR、改动命中高风险路径的合并决策简报(merge-decision.md)、CI 必需检查失败、接入问题、等待部署、
待处理的学习建议(控制措施与改进建议带决定文档；接受只记录决定，配置与提示由用户修改)。
排序：处理标签为立即修的置顶(按严重度)，其余保持列出顺序。运行摘要、每日汇总(digest.py)与 status 都从这里读取；
GitHub 镜像中待决定的 Issue 带 needs-decision 状态标签(pipeline/issue/steps/github.py)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from tightrein.domain.enums import (
    Disposition,
    IssuePhase,
    IssueStatus,
    OperationKind,
    OperationStatus,
    RunStage,
    Severity,
    SuggestionStatus,
    Treatment,
)
from tightrein.domain.issue import Issue, in_phase
from tightrein.domain.triage import urgency_key
from tightrein.orchestrator import resume
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.fix.steps import split
from tightrein.pipeline.triage.steps.select import TRIAGEABLE
from tightrein.store.files import documents
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import issues, onboarding_items, pending_operations, problems, pulls, suggestions, triage

MERGE_DECISION = "merge-decision"
CI_FAILED = "ci-failed"
ONBOARDING = "onboarding"
LEARN_SUGGESTION = "learn-suggestion"
SUGGESTION_ADVICE = ("阅读建议的证据(与决定文档)后接受或拒绝", "学习建议只在用户接受后生效，本工具不自动修改配置与提示")
GATE_ORDER = (resume.ISSUE_APPROVAL, resume.INTERACTIVE_FIX, resume.PR_REVIEW, resume.AWAITING_DEPLOY)
# 推荐做法与理由
OPERATION_ADVICE = {
    OperationKind.REVERT_PULL_REQUEST: ("先核对回归现象：确认由这次合并引起就确认并合并撤销 PR，否则拒绝",
                                        "部署后确认发现回归"),
    OperationKind.LOCAL_MIGRATION: ("核对迁移内容可在测试库回滚后确认", "启动本机服务会把迁移应用到测试库"),
    OperationKind.CLEANUP: ("PR 已合并且部署后确认通过时确认清理", "删除分支与 worktree 必须交用户(关卡 delete)"),
}
OPERATION_DEFAULT = ("核对操作内容后确认执行，不同意时拒绝并说明", "写操作按关卡表需要逐次确认")
GATE_ADVICE = {
    resume.FIX_PLAN: ("审阅修复计划(data/fixes/<编号>/plan.md)，范围合适时确认；需要调整时用 --reject --note 说明",
                      "计划不满足自动确认的规则，或关卡 plan-confirm 为 user"),
    resume.MANUAL_QUEUE: ("补充复现条件或证据后重新分诊", "取证证据不足或需要人工判断"),
    resume.ISSUE_APPROVAL: ("核对问题与范围后放行", "分诊判为需要修复，放行由用户决定(关卡 issue-approve)"),
    resume.PR_REVIEW: ("审阅 PR 与评审评论，没有问题即合并", "合并由用户决定(关卡 merge)或自动合并的条件尚未满足"),
    resume.AWAITING_DEPLOY: ("无需操作", "等待部署与部署后确认"),
}
HELD_ADVICE = ("查看停下的原因(Issue 历史与当天汇总)，处理后强制继续修复，或关闭 Issue",
               "无人值守修复停下、熔断或其他需要用户决定的原因")
FIX_ADVICE = ("在终端启动修复会话", "关卡 fix-session 为 user，修复需要交互会话")


def _issue_gate(issue: Issue) -> tuple[str, str] | None:
    """Issue 等待用户的关口与命令：待放行(含还没有修复分支的用户需求)、待决定后继续、交互修复、审核 PR、等待部署。"""
    if issue.status is IssueStatus.NEEDS_DECISION:
        if issue.hold is not None:
            return resume.INTERACTIVE_FIX, "tightrein fix start {n} --force"
        return resume.ISSUE_APPROVAL, "tightrein approve {n}"
    if issue.status is IssueStatus.TODO:
        if issue.is_manual and issue.branch is None:
            return resume.ISSUE_APPROVAL, "tightrein approve {n}"
        return resume.INTERACTIVE_FIX, "tightrein fix start {n}"
    if in_phase(issue, IssuePhase.FIX):
        return resume.INTERACTIVE_FIX, "tightrein fix start {n}"
    if issue.status is IssueStatus.PENDING_MERGE:
        return resume.PR_REVIEW, "{url}"
    if in_phase(issue, IssuePhase.DEPLOY_CHECK):
        return resume.AWAITING_DEPLOY, "tightrein show {n}"
    return None


def _number(issue_id: str) -> str:
    return issue_id.lstrip("0") or "0"


def _triaged(conn: sqlite3.Connection, problem_ids: Sequence[str]) -> tuple[Treatment | None, Severity | None]:
    """关联问题最近一次分诊中最紧迫的处理标签与严重度。"""
    found = [record.result for record in (triage.latest(conn, pid) for pid in problem_ids) if record is not None]
    if not found:
        return None, None
    first = min(found, key=lambda result: urgency_key(result.treatment, result.severity))
    return first.treatment, first.severity


def _item(kind: str, subject: str, summary: str, command: str, advice: tuple[str, str],
          treatment: Treatment | None = None, severity: Severity | None = None) -> dict[str, Any]:
    return {"kind": kind, "subjectId": subject, "summary": summary, "command": command,
            "recommendation": advice[0], "reason": advice[1], "treatment": None if treatment is None else treatment.value,
            "severity": None if severity is None else severity.value}


def _decision_brief(layout: WorkspaceLayout, path: str) -> str:
    """决策简报中的推荐与理由；读不到时给出通用说明。"""
    try:
        parsed = documents.read(layout.root / path)
    except (OSError, documents.DocumentError):
        return "审阅改动涉及的高风险文件后决定是否合并"
    return parsed.sections.get("recommendation") or "审阅改动涉及的高风险文件后决定是否合并"


def _release_items(conn: sqlite3.Connection, layout: WorkspaceLayout, issue: Issue) -> list[dict[str, Any]]:
    found = stage_runs.latest_outputs(conn, layout, RunStage.RELEASE, issue.id)
    merge = (found[1].get("autoMerge") or {}) if found is not None else {}
    pull = pulls.get(conn, issue.id)
    url = pull.url if pull is not None else f"tightrein issue show {issue.id}"
    items = []
    if merge.get("decision"):
        items.append(_item(MERGE_DECISION, issue.id, f"{issue.title}：改动涉及高风险路径，需要决定是否合并",
                           f"{merge['decision']}；{url}", (_decision_brief(layout, merge["decision"]),
                                                          "改动涉及高风险路径，合并必须交用户(关卡 high-risk-merge)"),
                           issue.treatment, issue.severity))
    ci = merge.get("ci") or {}
    if ci.get("state") == "failed":
        items.append(_item(CI_FAILED, issue.id, f"{issue.title}：CI 必需检查未通过 {'、'.join(ci['failed'])}", url,
                           ("查看失败的检查；由本次修复引起时 fix start --force 重新修复，CI 本身的问题由维护者处理",
                            "CI 必需检查失败时不自动合并"), issue.treatment, issue.severity))
    return items


def items(conn: sqlite3.Connection, layout: WorkspaceLayout | None = None) -> list[dict[str, Any]]:
    """全部待用户处理的事项。layout 为空时不读交接文档(决策简报与 CI 状态不列出)。"""
    found: list[dict[str, Any]] = []
    for record in pending_operations.find(conn, status=OperationStatus.PENDING):
        kind = resume.FIX_PLAN if record.kind is OperationKind.FIX_PLAN else resume.PENDING_OPERATION
        advice = GATE_ADVICE.get(kind) or OPERATION_ADVICE.get(record.kind, OPERATION_DEFAULT)
        found.append(_item(kind, record.id, f"{record.kind.label}(对象 {record.subject_id})",
                           f"tightrein approve {record.id}", advice))
    for problem in problems.find(conn, statuses=TRIAGEABLE):
        latest = triage.latest(conn, problem.id)
        if latest is not None and latest.result.disposition is Disposition.MANUAL_QUEUE:
            found.append(_item(resume.MANUAL_QUEUE, problem.id, problem.title,
                               f"tightrein problem retriage {problem.id} --note <补充信息>", GATE_ADVICE[resume.MANUAL_QUEUE],
                               latest.result.treatment, latest.result.severity))
    gated = [(record.issue, gate) for record in issues.find(conn) if (gate := _issue_gate(record.issue)) is not None]
    for issue, gate in sorted(gated, key=lambda pair: GATE_ORDER.index(pair[1][0])):
        if split.waiting_on(conn, issue) is not None:
            continue
        kind, command = gate
        pull = pulls.get(conn, issue.id)
        text = command.format(n=_number(issue.id), url=pull.url if pull else f"tightrein issue show {issue.id}")
        title = f"{issue.title}({issue.status.label}" + (f"：{issue.hold.reason}" if issue.hold else "") + ")"
        treatment, _ = _triaged(conn, issue.problems)
        advice = HELD_ADVICE if issue.hold is not None else GATE_ADVICE.get(kind, FIX_ADVICE)
        found.append(_item(kind, issue.id, title, text, advice, issue.treatment or treatment, issue.severity))
        if layout is not None and issue.status is IssueStatus.PENDING_MERGE:
            found += _release_items(conn, layout, issue)
    for question in onboarding_items.all_items(conn):
        if question.state == "blocked":
            found.append(_item(ONBOARDING, question.item, f"接入：{question.title}",
                               f"tightrein project answer {question.item} --recommended",
                               (question.recommendation or "按说明补充配置", "接入清单需要用户回答")))
        elif question.state == "failed":
            found.append(_item(ONBOARDING, question.item, f"接入失败：{question.title}：{question.detail}",
                               "tightrein project check", ("按失败原因处理后重新检查", "接入清单中的检查没有通过")))
    for record in suggestions.find(conn, status=SuggestionStatus.PENDING):
        advice = record.evidence.get("advice") or {}
        where = f"(决定文档 {record.target_path})" if record.target_path else ""
        found.append(_item(LEARN_SUGGESTION, record.id, f"{record.kind.label}：{record.subject}{where}",
                           f"tightrein learn accept {record.id}",
                           (advice.get("recommendation") or SUGGESTION_ADVICE[0],
                            advice.get("reason") or SUGGESTION_ADVICE[1])))
    pinned = [item for item in found if item["treatment"] == Treatment.IMMEDIATE.value]
    ordered = sorted(pinned, key=lambda item: urgency_key(Treatment.IMMEDIATE, Severity(item["severity"])
                                                          if item["severity"] else None))
    return [*ordered, *(item for item in found if item not in pinned)]


def needs_user(found: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """真正需要用户动手的事项(去掉等待部署)。"""
    return [item for item in found if item["kind"] != resume.AWAITING_DEPLOY]

