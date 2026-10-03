"""直接执行的待确认操作(architecture/02 4.5，redesign/09-loop.md 第 2 节)。

关卡 gates.release-writes 为 auto 的项目，建修复分支、本地提交、合并主干、推送修复分支、创建或更新 PR、在 PR 上发评审评论、
提撤销合并的 PR 生成后立即由 OperationRunner.run_unattended 执行，不等用户确认；删除 worktree 与本地分支属于必须交用户的
关卡 delete，冲突解决后的合并提交与放弃合并也不在其中。自动确认的修复计划(gates.plan-confirm)与自动合并 PR(gates.merge)
同样经 run_unattended 执行，由发起模块先判断规则。
"""

from __future__ import annotations

from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import OperationKind

RELEASE_KINDS = frozenset({OperationKind.CREATE_FIX_WORKTREE, OperationKind.COMMIT, OperationKind.MERGE_MAIN,
                           OperationKind.PUSH, OperationKind.PULL_REQUEST, OperationKind.PR_COMMENT,
                           OperationKind.REVERT_PULL_REQUEST})
KINDS = RELEASE_KINDS | {OperationKind.FIX_PLAN, OperationKind.MERGE_PULL_REQUEST}
BRANCH_KINDS = frozenset({OperationKind.COMMIT, OperationKind.MERGE_MAIN, OperationKind.PUSH})
RELEASE_REASON = f"{gates.reason(Gate.RELEASE_WRITES)}：本项目的建分支、提交、推送与提 PR 不逐次确认"


def release_direct(config: ProjectConfig, kind: OperationKind) -> bool:
    """该操作在本项目是否生成后直接执行。"""
    return kind in RELEASE_KINDS and gates.auto(config, Gate.RELEASE_WRITES)
