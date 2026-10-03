"""审批关卡表(redesign/09-loop.md 第 2 节)：哪些事项交用户决定、哪些按规则由程序处理，集中写在配置的 gates 段。

每个关卡取 user(交用户)或 auto(按规则自动)；必须交用户的五项(MANDATORY)在配置 schema 中只允许 user，这里再按
MANDATORY 判断一次，模型与配置都不能把它们改为自动。各关卡的规则与接入位置：

| 关卡 | auto 时 | 接入位置 |
|---|---|---|
| issue-approve | 按 autonomy.approve 放行新建 Issue(用户需求创建即放行) | pipeline/issue/service.py |
| plan-confirm | B 通道的计划按自主确认规则确认(A 总是自动、C 总是交用户) | FixService.auto_confirm |
| fix-session | run 以非交互任务推进已放行的 Issue | orchestrator/service.py、rules.py |
| release-writes | 建分支、提交、合并主干、推送、提 PR、PR 评论、撤销 PR 直接执行 | vcs/unattended.py |
| mirror-writes | GitHub Issue 镜像的写入直接执行 | pipeline/issue/steps/github.py |
| merge | 满足自动合并条件时合并 PR | ReleaseService._track_pull |
| high-risk-merge | — 改动命中 release.autoMergeBlockPaths 时写决策简报交用户 | ReleaseService._auto_merge |
| delete | — 删除修复 worktree、分支或数据一律待确认 | vcs/unattended.py 不放行 |
| permissions-secrets | — 计划改动 credentialFiles 或 review.riskRules.authz 命中的文件时交用户 | autonomy.plan_confirmation |
| over-task-limit | — 超出单个任务上限(thresholds.change、超限档)转待决定 | 修复第 0、3、6 步 |
| needs-decision | — 待决定的 Issue 不被自动推进 | rules.unattended_issues |
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from tightrein.config.project import ProjectConfig

USER = "user"
AUTO = "auto"


class Gate(Enum):
    ISSUE_APPROVE = "issue-approve"
    PLAN_CONFIRM = "plan-confirm"
    FIX_SESSION = "fix-session"
    RELEASE_WRITES = "release-writes"
    MIRROR_WRITES = "mirror-writes"
    MERGE = "merge"
    HIGH_RISK_MERGE = "high-risk-merge"
    DELETE = "delete"
    PERMISSIONS_SECRETS = "permissions-secrets"
    OVER_TASK_LIMIT = "over-task-limit"
    NEEDS_DECISION = "needs-decision"


MANDATORY = frozenset({Gate.HIGH_RISK_MERGE, Gate.DELETE, Gate.PERMISSIONS_SECRETS, Gate.OVER_TASK_LIMIT,
                       Gate.NEEDS_DECISION})


def auto(config: ProjectConfig, gate: Gate) -> bool:
    """该关卡在本项目是否按规则自动处理；必须交用户的关卡总是 False。"""
    return gate not in MANDATORY and config.get(f"gates.{gate.value}") == AUTO


def reason(gate: Gate) -> str:
    """直接执行时写进事件与历史的理由。"""
    return f"gates.{gate.value} 为 auto"


def table(config: ProjectConfig) -> list[dict[str, Any]]:
    """当前生效的关卡表(config show 与 loop skill 展示)。"""
    return [{"gate": gate.value, "decide": AUTO if auto(config, gate) else USER, "mandatory": gate in MANDATORY}
            for gate in Gate]
