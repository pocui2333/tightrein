"""命令树：日常命令在顶层，其余按用途收进 issue、problem、project、admin 四组，流水线单步命令在顶层(根帮助中标为高级)。
每个模块提供 register(commands, common)，给叶子命令设置 handler 与 command_name(完整路径)。"""

from typing import Any

from tightrein.cli.commands import (
    collect,
    config,
    delivery,
    doc,
    eval_,
    ext,
    install,
    issue,
    kb,
    learn,
    loop,
    probe,
    schedule,
    skills,
    third_party,
    triage,
    watch,
    workspace,
    worktree,
)
from tightrein.cli.commands.common import group


def register_all(commands: Any, common: Any) -> None:
    loop.register(commands, common)
    watch.register(commands, common)
    issue.register_new(commands, common)
    issue.register(commands, common)
    problem = group(commands, "problem", "发现的问题：忽略、误报、合并、重开、重新分诊")
    collect.register_problem(problem, common)
    triage.register_problem(problem, common)
    project = group(commands, "project", "接入与配置项目：初始化、探针、接口描述、worktree、配置、定时")
    for module in (workspace, probe, worktree, config, schedule):
        module.register(project, common)
    admin = group(commands, "admin", "维护 tightrein：安装、第三方 skill、检查、评测、知识库、扩展")
    for module in (install, third_party, skills, doc, eval_, kb, ext):
        module.register(admin, common)
    for module in (collect, triage, delivery, learn):
        module.register(commands, common)
