"""各命令组：每个模块提供 register(commands, common)，给叶子命令设置 handler 与 command_name。"""

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

COMMAND_GROUPS = (loop, collect, probe, triage, issue, delivery, learn, kb, worktree, ext, eval_, config, install,
                  third_party, schedule, skills, doc, workspace, watch)
