"""install、uninstall(architecture/09 6.3)：把 skills 安装到 Claude Code、Codex CLI、Antigravity CLI(agy)，或卸载本工具安装的条目。

install 有冲突、检查不通过时停止并列出全部问题；--dry-run 只列出将要做的事；--check 只核对。uninstall 列出将删除的
条目，终端中输入 yes 后才执行，非交互调用停在关口。--repo-only(与 --tool 互斥)只处理本工具仓库内 `skills/<名称>` 的
第三方 skill 链接与下载缓存，不构建插件、不碰任何工具目录。
"""

from __future__ import annotations

import argparse
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import confirmed, leaf, not_confirmed, packaging_context
from tightrein.cli.output import Outcome, error
from tightrein.packaging import install

TOOL_CHOICES = (*install.TOOLS, install.ALL)


def _targets(invocation: Any, ctx: install.Context) -> list[install.Target]:
    if invocation.args.repo_only:
        return []
    return install.targets(ctx.config, ctx.user, ctx.home, invocation.args.tool or install.ALL)


def _failed(name: str, failure: install.InstallError) -> Outcome:
    return Outcome(name, exit_codes.FAILED, [f"{name} 未完成", *failure.problems],
                   result={"problems": failure.problems},
                   errors=[error("InstallError", problem) for problem in failure.problems])


def _plan_lines(plan: install.Plan) -> list[str]:
    return [f"- {action.describe()}" for action in plan.actions] + plan.notes


def _install(invocation: Any) -> Outcome:
    args = invocation.args
    ctx = packaging_context(invocation)
    chosen = _targets(invocation, ctx)
    if args.check:
        items = install.check(ctx, chosen)
        bad = [item for item in items if item.status != install.OK]
        lines = [f"- {item.tool} {item.name}：{item.status} {item.detail}".rstrip() for item in bad]
        head = "安装核对通过" if not bad else f"安装核对发现 {len(bad)} 项需要处理(重新执行 tightrein install)"
        return Outcome("install --check", exit_codes.FAILED if bad else exit_codes.OK, [head, *lines],
                       result=[item.to_dict() for item in items])
    try:
        plan = install.install(ctx, chosen, dry_run=args.dry_run)
    except install.InstallError as failure:
        return _failed("install", failure)
    if not plan.actions:
        head = "已是最新，没有需要做的事"
    else:
        head = "将要执行：" if args.dry_run else f"已安装到 {', '.join(target.tool for target in chosen) or install.REPO}"
    return Outcome("install", exit_codes.OK, [head, *_plan_lines(plan)], result=plan.to_dict())


def _uninstall(invocation: Any) -> Outcome:
    ctx = packaging_context(invocation)
    chosen = _targets(invocation, ctx)
    repo_only = invocation.args.repo_only
    plan, _ = install.plan_uninstall_repo(ctx) if repo_only else install.plan_uninstall(ctx, chosen)
    if plan.problems:
        return _failed("uninstall", install.InstallError(plan.problems))
    lines = _plan_lines(plan)
    if not plan.actions:
        if not invocation.args.dry_run:
            install.uninstall(ctx, chosen, repo_only=repo_only)
        return Outcome("uninstall", exit_codes.OK, ["没有本工具安装的条目需要删除"], result=plan.to_dict())
    if invocation.args.dry_run:
        return Outcome("uninstall", exit_codes.OK, ["将要执行：", *lines], result=plan.to_dict())
    if not confirmed(invocation, "\n".join(["将删除：", *lines])):
        if not invocation.interactive:
            return not_confirmed("uninstall", lines, plan.to_dict())
        return Outcome("uninstall", exit_codes.OK, ["已取消"], result=plan.to_dict())
    try:
        install.uninstall(ctx, chosen, repo_only=repo_only)
    except install.InstallError as failure:
        return _failed("uninstall", failure)
    return Outcome("uninstall", exit_codes.OK, ["已卸载", *lines], result=plan.to_dict())


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    parser = leaf(commands, common, "install", _install, "把 skills 安装到 agent 工具")
    _scope(parser, "安装到哪个工具", "只在本工具仓库的 skills/ 下放置第三方 skill，不碰任何工具目录")
    parser.add_argument("--check", action="store_true", help="只核对已安装的条目")
    parser = leaf(commands, common, "uninstall", _uninstall, "卸载本工具安装的 skills")
    _scope(parser, "从哪个工具卸载", "只删除本工具仓库 skills/ 下第三方 skill 的链接")


def _scope(parser: argparse.ArgumentParser, tool_help: str, repo_help: str) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tool", choices=TOOL_CHOICES, help=f"{tool_help}(缺省 {install.ALL})")
    group.add_argument("--repo-only", action="store_true", help=repo_help)
