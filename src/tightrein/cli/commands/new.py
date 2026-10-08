"""`tightrein new "<需求描述>" [--severity P0..P3] [--type bug|feature]`：用户自己提需求，直接进评估写成 Issue。

记一次手动运行(runs 表，阶段 assess)：评估要调用模型，watch 与 status 能看到它在跑；中途出错由收尾标为失败。
"""

from __future__ import annotations

import argparse

from tightrein.cli.exit_codes import UsageError
from tightrein.cli.session import Result, Session, add_command
from tightrein.cli.text import text

SEVERITIES = ("P0", "P1", "P2", "P3")
KINDS = ("bug", "feature")
STAGE = "assess"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    parser = add_command(commands, "new", common=common, language=language, help_key="help.new", handler=handle)
    parser.add_argument("description", metavar="<text>", help=text(language, "help.arg_text"))
    parser.add_argument("--severity", choices=SEVERITIES, help=text(language, "help.new_severity"))
    parser.add_argument("--type", dest="kind", choices=KINDS, default="feature", help=text(language, "help.new_type"))


def handle(session: Session) -> Result:
    from tightrein.assess.issue.create import new_issue  # 评估阶段；延迟导入，别的命令不必加载评估

    args = session.args
    description = args.description.strip()
    if not description:
        raise UsageError(session.text("cmd.new_needs_text"))
    if not session.confirm([session.text("cmd.new_action", kind=args.kind, severity=args.severity or "-")]):
        return session.declined()
    current = session.begin(STAGE)
    issue = new_issue(current, description, severity=args.severity, kind=args.kind)
    session.end(current)
    return Result(session.command, lines=[session.text("cmd.new_created", subject=issue)],
                  data={"issue": issue, "run": current.run}, next=f"tightrein show {issue}")
