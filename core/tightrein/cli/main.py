"""tightrein 的命令行入口(architecture/09 4.1 到 4.5)。

通用参数以父解析器挂到每个叶子命令上(写在子命令之后)；参数错误抛出 UsageError(退出码 2)，--json 时同样只向
标准输出写一个 JSON 对象。每个命令的处理函数接收 Invocation、返回 Outcome；异常按 exit_codes.for_error 映射，
未映射的异常为 1，并给出事件日志路径。main 不调用 sys.exit，console script 的入口是 entry。
dispatch 以已打开的 App 执行一条子命令，供编排执行定时任务的 command。
中断：entry 把 SIGTERM、SIGHUP 转成 exit_codes.Terminated，与 Ctrl+C 的 KeyboardInterrupt 一样沿调用栈向上抛出(沿途的
finally 与上下文管理器照常执行，子进程启动器终止子进程组)；main 输出「被中断」，退出码 130 或 128 + 信号编号。
main 在关闭数据库前调用 recovery.close_own 收尾本进程仍为进行中的运行并释放本进程的对象锁(architecture/09 3.5)。
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import FrameType
from typing import NoReturn, TextIO

from tightrein import __version__
from tightrein.cli import exit_codes
from tightrein.cli.assemble import App, Externals, Options
from tightrein.cli.commands import register_all
from tightrein.cli.commands.common import DRY_RUN_COMMANDS, PROG, CommonParsers, root_help
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome, emit, error
from tightrein.domain.enums import RunStatus
from tightrein.orchestrator import recovery
from tightrein.runner.roles import Overrides
from tightrein.store import locks

# 已改名的命令：老写法(按开头的一到两个词匹配) → 新写法；敲老写法时报用法错误并给出新写法，不执行
RENAMED = {
    ("next",): "show", ("pending",): "status --pending", ("confirm",): "approve",
    ("issue", "approve"): "approve", ("ignore",): "problem ignore", ("false-positive",): "problem false-positive",
    ("merge",): "problem merge", ("reopen",): "problem reopen", ("retriage",): "problem retriage",
    ("workspace",): "project init / check / answer", ("probe",): "project probe", ("spec",): "project spec",
    ("worktree",): "project worktree", ("config",): "project config", ("schedule",): "project schedule",
    ("install",): "admin install", ("uninstall",): "admin uninstall", ("third-party",): "admin third-party",
    ("skills",): "admin skills", ("doc",): "admin doc", ("eval",): "admin eval", ("kb",): "admin kb",
    ("ext",): "admin ext",
}
MANUAL_RENAMED = "new"  # issue create --manual


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


class RootParser(Parser):
    def format_help(self) -> str:
        return root_help(self.description or "", self._subparsers._group_actions[0])  # type: ignore[union-attr]


def common_parser(*, dry_run: bool = False) -> argparse.ArgumentParser:
    """帮助只显示人会用到的参数；其余(评测、回放、编排用的)照常接受，帮助中不列出。"""
    hidden = argparse.SUPPRESS
    common = CommonParsers(add_help=False)
    common.add_argument("-w", "--workspace", type=Path, help="工作区的路径或项目名；省略时取本机用户配置的 defaultWorkspace")
    common.add_argument("--json", action="store_true", help="以 JSON 输出")
    common.add_argument("--dry-run", action="store_true", help="只列出将要做什么" if dry_run else hidden)
    common.add_argument("--verbose", action="store_true", help="人读输出附带步骤明细")
    common.add_argument("--now", help=hidden)
    common.add_argument("--select", action="append", default=[], help=hidden)
    common.add_argument("--input", type=Path, help=hidden)
    common.add_argument("--output", type=Path, help=hidden)
    common.add_argument("--ignore-state", action="store_true", help=hidden)
    common.add_argument("--runner", help=hidden)
    common.add_argument("--model", help=hidden)
    common.add_argument("--replay-from", help=hidden)
    common.add_argument("--target", help=hidden)
    common.add_argument("--commit", help=hidden)
    common.add_argument("--gate-decisions", type=Path, help=hidden)
    if not dry_run:
        common.dry_run = common_parser(dry_run=True)
    return common


@lru_cache(maxsize=1)
def build_parser() -> argparse.ArgumentParser:
    """解析器只在第一次调用时构造；parse_args 不改变它，dispatch 与 main 共用。"""
    parser = RootParser(prog=PROG, description="tightrein：自动发现并受控修复代码缺陷")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser, metavar="<命令>")
    register_all(commands, common_parser())
    return parser


def renamed(arguments: Sequence[str]) -> str | None:
    """老写法的提示；不是老写法时为空。"""
    words = [item for item in arguments if not item.startswith("-")][:2]
    if words[:2] == ["issue", "create"] and "--manual" in arguments:
        return f"`issue create --manual` 已改为 `{MANUAL_RENAMED}`"
    for key in (tuple(words[:2]), tuple(words[:1])):
        if key in RENAMED:
            return f"`{' '.join(key)}` 已改为 `{RENAMED[key]}`"
    return None


@dataclass
class Invocation:
    args: argparse.Namespace
    externals: Externals
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    shared: App | None = None
    _app: App | None = field(default=None, repr=False)
    # 命令以什么结束：None 为正常返回，否则为捕获到的异常或中断(close 据此决定收尾的状态)
    failure: BaseException | None = field(default=None, repr=False)

    @property
    def app(self) -> App:
        if self.shared is not None:
            return self.shared
        if self._app is None:
            args = self.args
            self._app = App(Options(args.workspace, args.now, args.output, args.runner, args.model,
                                    args.replay_from, args.target, args.commit), self.externals)
            self._app.stderr = self.stderr
        return self._app

    @property
    def json(self) -> bool:
        return bool(self.args.json)

    @property
    def interactive(self) -> bool:
        """标准输入是终端且没有用 --json：可以当场询问用户。"""
        return not self.json and self.externals.stdin_is_tty()

    @property
    def overrides(self) -> Overrides:
        return Overrides(self.args.runner, self.args.model)

    def close(self) -> None:
        if self._app is not None:
            self._close_own_runs(self._app)
            self._app.close()

    def _close_own_runs(self, app: App) -> None:
        if app.output_mode:
            return
        failure = self.failure
        if isinstance(failure, (KeyboardInterrupt, exit_codes.Terminated)):
            status, reason = RunStatus.INTERRUPTED, "命令被中断"
        elif failure is not None:
            status, reason = RunStatus.FAILED, f"命令出错：{type(failure).__name__}"
        else:
            status, reason = RunStatus.FAILED, "命令结束时运行仍为进行中"
        try:
            recovery.close_own(app.conn, app.clock, app.events, holder=locks.current_holder(), status=status,
                               reason=reason)
        except Exception as problem:  # noqa: BLE001 收尾失败不覆盖原来的结果，下一次 run 的中断恢复兜底
            self.stderr.write(f"收尾本进程的运行失败：{type(problem).__name__}: {problem}\n")


def _checked(args: argparse.Namespace) -> None:
    """--output 下 triage、issue、fix 都没有需要用户的关口，--gate-decisions 只校验文件存在。
    --dry-run 只给实现了预览的命令，其余命令收到时报错，不静默忽略后照常执行。"""
    name = getattr(args, "command_name", args.command)
    if args.dry_run and name not in DRY_RUN_COMMANDS:
        raise UsageError(f"{name} 不支持 --dry-run；支持的命令：{'、'.join(sorted(DRY_RUN_COMMANDS))}")
    if args.ignore_state and args.output is None:
        raise UsageError("--ignore-state 只能与 --output 同用")
    if args.gate_decisions is not None and (args.output is None or not args.gate_decisions.is_file()):
        raise UsageError("--gate-decisions 只能与 --output 同用，且须是已存在的文件")


def execute(invocation: Invocation) -> Outcome:
    args = invocation.args
    name = getattr(args, "command_name", args.command)
    try:
        _checked(args)
        outcome = args.handler(invocation)
        if not outcome.command.startswith(name):
            outcome.command = name  # 处理函数里写的是改名前的叫法时，输出用当前的命令名
        return outcome
    except Exception as failure:  # noqa: BLE001 命令的最外层：映射为退出码并输出，不向终端抛出堆栈
        invocation.failure = failure
        code = exit_codes.for_error(failure)
        hint = None
        if code == exit_codes.FAILED and invocation._app is not None:
            app = invocation._app
            hint = f"事件日志：{app.layout.relative(app.layout.events_log(app.clock.now().date()))}"
        return Outcome(name, code, [f"{name} 未完成"], errors=[error(type(failure).__name__, str(failure), hint)])


def main(argv: Sequence[str] | None = None, externals: Externals | None = None, *, stdin: TextIO | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv) or ["status"]
    out, err = stdout or sys.stdout, stderr or sys.stderr
    try:
        hint = renamed(arguments)
        if hint is not None:
            raise UsageError(hint)
        args = build_parser().parse_args(arguments)
    except UsageError as failure:
        outcome = Outcome(" ".join(arguments[:1]) or PROG, exit_codes.USAGE, [f"用法错误：{failure}"],
                          errors=[error("UsageError", str(failure), f"执行 {PROG} --help 查看用法")])
        emit(outcome, "--json" in arguments, out)
        return outcome.exit_code
    except SystemExit as exited:
        return int(exited.code or 0)
    invocation = Invocation(args, externals or Externals(), stdin or sys.stdin, out, err)
    try:
        outcome = execute(invocation)
    except (KeyboardInterrupt, exit_codes.Terminated) as stopped:
        invocation.failure = stopped
        name = getattr(args, "command_name", args.command)
        outcome = Outcome(name, exit_codes.for_interrupt(stopped), [f"{name} 被中断"])
    finally:
        invocation.close()
    emit(outcome, invocation.json, out)
    return outcome.exit_code


def dispatch(app: App, argv: Sequence[str]) -> int:
    """以已打开的 App 执行一条子命令(定时任务)，人读输出写到标准错误，返回退出码。"""
    err = app.stderr
    try:
        args = build_parser().parse_args(list(argv))
    except UsageError as failure:
        err.write(f"定时任务的命令不合法：{' '.join(argv)}：{failure}\n")
        return exit_codes.USAGE
    invocation = Invocation(args, app.externals, sys.stdin, err, err, shared=app)
    outcome = execute(invocation)
    emit(outcome, False, err)
    return outcome.exit_code


def _terminate(signum: int, _frame: FrameType | None) -> None:
    raise exit_codes.Terminated(signum)


def entry() -> None:
    """SIGTERM、SIGHUP 只在这里转成异常：测试直接调用 main，不改变测试进程的信号处理。"""
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, _terminate)
    sys.exit(main())

