"""worktree 的命令(architecture/09 4.1)：init 生成待确认操作；sync 切换只读 worktree；list 列出 worktree。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.confirm import pending_dict
from tightrein.cli.output import Outcome
from tightrein.sources.common import target as common_target


def _init(invocation: Any) -> Outcome:
    operation = invocation.app.planner().plan_init_readonly_worktree()
    item = pending_dict(operation)
    return Outcome("worktree init", exit_codes.GATE, ["创建只读 worktree 需要确认"], result=item, pending=[item])


def _sync(invocation: Any) -> Outcome:
    """省略 --commit 时取 staging 当前部署的 commit，还没有部署记录时取 origin/<主分支>。"""
    app = invocation.app
    commit = app.sync_readonly(invocation.args.commit or common_target.latest_release(app.conn))
    return Outcome("worktree sync", exit_codes.OK, [f"只读 worktree 已在 {commit[:12]}"], result={"commit": commit})


def _list(invocation: Any) -> Outcome:
    app = invocation.app
    found = app.git.worktree_list(app.config.repo)
    lines = [f"- {item.path} {item.head or ''} {item.branch or '(游离)'}" for item in found]
    return Outcome("worktree list", exit_codes.OK, lines or ["没有 worktree"], result=[asdict(item) for item in found])


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    group_ = group(commands, "worktree", "只读与修复 worktree")
    leaf(group_, common, "init", _init, "申请创建只读 worktree", "worktree init")
    leaf(group_, common, "sync", _sync, "把只读 worktree 切换到 commit(缺省为 staging 当前部署的 commit)", "worktree sync")
    leaf(group_, common, "list", _list, "列出 worktree", "worktree list")
