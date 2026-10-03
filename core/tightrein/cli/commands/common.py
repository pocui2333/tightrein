"""各命令组共用：注册叶子命令、构造编排器、把模块结果转为输出。"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.assemble import App, alternate_route, environment_for, parse_now, transport_for
from tightrein.cli.confirm import ask, load, pending_dict
from tightrein.cli.output import Outcome
from tightrein.config import project, user
from tightrein.config.network import Reroute
from tightrein.domain.clock import Clock, FixedClock, SystemClock
from tightrein.domain.enums import HandoffStatus
from tightrein.orchestrator.service import Orchestrator
from tightrein.store.files.layout import UserLayout
from tightrein.packaging import install, skills_check, third_party
from tightrein.vcs.process import VcsProcess

Handler = Callable[[Any], Outcome]


def leaf(commands: Any, common: argparse.ArgumentParser, name: str, handler: Handler, help_text: str,
         command_name: str | None = None) -> argparse.ArgumentParser:
    parser = commands.add_parser(name, parents=[common], help=help_text, description=help_text)
    parser.set_defaults(handler=handler, command_name=command_name or name)
    return parser


def group(commands: Any, name: str, help_text: str) -> Any:
    parser = commands.add_parser(name, help=help_text, description=help_text)
    return parser.add_subparsers(dest=f"{name}_command", required=True, parser_class=type(parser))


def orchestrator(app: App) -> Orchestrator:
    return Orchestrator(app, layout=app.layout, conn=app.conn, config=app.config, clock=app.clock,
                        events=app.events, notifier=app.notifier, zone=app.zone, alive=app.externals.alive,
                        reroutes=lambda: [item.text() for item in app.process.reroutes],
                        pause_flag=UserLayout(app.home).pause_flag(), onboarding=app.onboarding())


def number(issue_id: str) -> str:
    return issue_id.lstrip("0") or "0"


def issue_id(text: str) -> str:
    """命令行中的 Issue 编号：`7` 或 `0007`。"""
    if not text.isdigit():
        raise exit_codes.UsageError(f"Issue 编号只能是数字：{text}")
    return f"{int(text):04d}"


def module_outcome(name: str, app: App, subject: str | None, result: Any, next_command: str | None = None,
                   extra: dict[str, Any] | None = None) -> Outcome:
    """fix、verify、release 的结果(status、message、可选的 operation、handoff)转为输出。"""
    status: HandoffStatus = result.status
    operation = getattr(result, "operation", None)
    code = exit_codes.for_status(status, operation)
    handoff = getattr(result, "handoff", None)
    values: dict[str, Any] = {"status": status.value, "message": result.message,
                              "handoff": app.layout.relative(handoff) if handoff else None}
    values.update(extra or {})
    pending = [pending_dict(load(app, operation))] if operation else []
    return Outcome(name, code, [result.message], {"type": "issue", "id": subject} if subject else None, values,
                   next=next_command, pending=pending)


def confirmed(invocation: Any, text: str) -> bool:
    """终端中展示 text 后输入 yes 才同意；非交互调用(--json 或标准输入不是终端)不询问，视为未同意。"""
    return bool(invocation.interactive) and ask(text, invocation.stdin, invocation.stdout)


def not_confirmed(name: str, lines: list[str], result: Any = None) -> Outcome:
    """需要用户在终端确认的命令在非交互调用中停在关口(退出码 4)。"""
    return Outcome(name, exit_codes.GATE, [f"{name} 需要用户确认", *lines], result=result,
                   next=f"在终端中执行 tightrein {name}，确认后输入 yes")


def packaging_context(invocation: Any) -> install.Context:
    """不需要工作区的命令(install、uninstall、third-party、skills check)：核心缺省配置、本机用户配置与工具根目录。"""
    from tightrein.cli.main import build_parser  # main 导入各命令组，这里在调用时导入

    ext = invocation.externals
    clock: Clock = FixedClock(parse_now(invocation.args.now, ext.zone)) if invocation.args.now else SystemClock()
    config = project.core_config()
    settings = user.load(home=ext.home)
    environ = environment_for(ext, settings)

    def record(reroute: Reroute) -> None:
        invocation.stderr.write(f"网络换路：{reroute.text()}\n")

    process = VcsProcess(execute=ext.vcs_execute, environ=environ, sleep=ext.sleep, on_reroute=record)
    fetch = third_party.fetcher(transport_for(ext, settings, environ),
                                float(config.get("packaging.downloadTimeoutSeconds")),
                                alternate_route(ext, config, environ, record))
    return install.Context(ext.tool(), ext.home, settings, config, clock, ext.zone, process, fetch,
                           skills_check.command_tree(build_parser()))
