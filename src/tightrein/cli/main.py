"""命令行入口(cli/README.md)：解析、分发、输出与收尾。

- 全局选项 -p/--project、--json、--lang、--yes、-h 挂在每个命令上(写在命令之后)；不带命令时执行 status；
- 参数错误、改了名的老命令都抛 UsageError(退出码 2)，--json 时同样只向标准输出写一个 JSON 对象；
- 命令抛出的异常按类型映射退出码(exit_codes.for_error)，不向终端抛堆栈；
- 中断：只在 entry 里把 SIGTERM、SIGHUP 转成 protocol.process.Interrupted(与 KeyboardInterrupt 同级，不会被
  `except Exception` 吞掉)，沿调用栈抛出，沿途 finally 与子进程组终止照常执行；退出码 Ctrl-C 130、信号 128+编号。
  测试直接调用 main，不改测试进程的信号处理；
- 命令结束时就地收尾(protocol.recovery.close_own)：本进程开始、仍在进行的运行标为中断或失败，释放本进程的锁；
  收尾在宽限期内做完，收尾本身出错不覆盖命令原来的结果，由下次启动恢复兜底。
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable, Sequence
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import FrameType
from typing import NoReturn, TextIO, cast

from tightrein.cli import exit_codes
from tightrein.cli.assemble import Externals
from tightrein.cli.commands import COMMANDS
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.session import DEFAULT_LANGUAGE, Result, Session, emit, error, pick_language
from tightrein.cli.text import LANGUAGES, text
from tightrein.protocol import recovery
from tightrein.protocol.process import Interrupted
from tightrein.protocol.records import EventLog
from tightrein.protocol.security import Redactor
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.locks import FileLock

PROG = "tightrein"
DEFAULT_COMMAND = "status"
NOT_A_COMMAND = ("-h", "--help", "--version")
FALLBACK_GRACE_S = 30.0  # 配置读不出时收尾的宽限

# 取消或改名的老命令(按开头的一到两个词匹配) → 新写法；敲老写法时报用法错误并给出新写法，不执行
RENAMED: dict[tuple[str, ...], str] = {
    ("continue",): "run", ("find",): "show --search", ("next",): "show", ("pending",): "status",
    ("confirm",): "approve", ("collect",): "run collect", ("aggregate",): "run collect", ("triage",): "run assess",
    ("fix",): "run implement", ("verify",): "run release", ("release",): "run release", ("learn",): "run retro",
    ("tick",): "run", ("issue",): "show / approve / reject", ("ignore",): "problem mute",
    ("workspace",): "project", ("probe",): "project check", ("spec",): "project check",
    ("schedule",): "project ready", ("config",): "project config", ("install",): "admin install",
    ("uninstall",): "admin uninstall", ("kb",): "knowledge", ("doc",): "show --doc",
    ("problem", "ignore"): "problem mute", ("admin", "third-party"): "vendor/README.md",
    ("admin", "skills"): "admin install", ("admin", "eval"): "-", ("admin", "ext"): "-", ("admin", "kb"): "knowledge",
}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def entry() -> None:
    """SIGTERM、SIGHUP 只在这里转成 Interrupted：测试直接调用 main，不改变测试进程的信号处理。"""
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, _terminate)
    sys.exit(main())


def main(argv: Sequence[str] | None = None, externals: Externals | None = None, *, stdin: TextIO | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    arguments = with_default_command(list(sys.argv[1:] if argv is None else argv))
    out, err = stdout or sys.stdout, stderr or sys.stderr
    json_mode = "--json" in arguments
    language = _language_of(arguments)
    try:
        hint = renamed(arguments)
        if hint is not None:
            raise UsageError(text(language, "cli.renamed", old=hint[0], new=hint[1]))
        args = build_parser(language).parse_args(arguments)
    except UsageError as failure:
        result = Result(arguments[0], exit_codes.USAGE, [text(language, "cli.usage_error", message=str(failure))],
                        errors=[error("UsageError", str(failure), text(language, "cli.usage_hint", prog=PROG))])
        emit(result, json_mode, out, language)
        return result.code
    except SystemExit as exited:  # -h、--version
        return int(exited.code or 0)
    session = Session(args, externals or Externals.real(), stdin or sys.stdin, out, err)
    try:
        result = execute(session)
    except (KeyboardInterrupt, Interrupted) as stopped:
        session.failure = stopped
        result = Result(session.command, exit_codes.for_interrupt(stopped),
                        [session.text("cli.interrupted", command=session.command)])
    finally:
        close(session, err)
    emit(result, session.json, out, session.language)
    return result.code


def execute(session: Session) -> Result:
    handler: Callable[[Session], Result] = session.args.handler
    try:
        return handler(session)
    except Exception as failure:  # noqa: BLE001 命令的最外层：映射为退出码并输出，不向终端抛出堆栈
        session.failure = failure
        return Result(session.command, exit_codes.for_error(failure),
                      [session.text("cli.not_done", command=session.command)],
                      errors=[error(type(failure).__name__, str(failure))])


def close(session: Session, stderr: TextIO) -> None:
    """就地收尾：本进程仍在进行的运行标为中断或失败，释放本进程的锁，关闭数据库。"""
    workspace = session.opened
    if workspace is None:
        return
    externals = session.externals
    layout, settings, conn = workspace.layout, workspace.settings, workspace.conn

    def action() -> None:
        stale_s = settings.duration("limits.lock.stale")
        lock_files = [layout.run_lock, *_object_locks(layout)]
        locks = [FileLock(path, externals.clock, stale_s=stale_s) for path in lock_files if path.exists()]
        recovery.close_own(conn, externals.clock, pid=externals.pid, host=externals.host, failure=session.failure,
                           locks=locks, events_for=lambda run: EventLog(layout.events(run), Redactor(), externals.clock))

    code = exit_codes.for_interrupt(session.failure) if isinstance(session.failure, (KeyboardInterrupt, Interrupted)) \
        else exit_codes.FAILED
    try:
        recovery.within_grace(_grace(session), action, code=code)
    except Exception as problem:  # noqa: BLE001 收尾失败不覆盖命令原来的结果，下次启动恢复兜底
        stderr.write(session.text("cli.close_failed", kind=type(problem).__name__, message=str(problem)) + "\n")
    finally:
        conn.close()


@cache
def build_parser(language: str) -> argparse.ArgumentParser:
    """解析器按语言只构造一次；parse_args 不改变它。"""
    parser = Parser(prog=PROG, description=text(language, "help.root"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    commands = cast("argparse._SubParsersAction[argparse.ArgumentParser]", parser.add_subparsers(
        dest="command", required=True, parser_class=Parser, metavar="<command>"))
    common = common_options(language)
    for register in COMMANDS:
        register(commands, common, language)
    return parser


def common_options(language: str) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--project", help=text(language, "help.option_project"))
    common.add_argument("--json", action="store_true", help=text(language, "help.option_json"))
    common.add_argument("--lang", choices=LANGUAGES, help=text(language, "help.option_lang"))
    common.add_argument("--yes", action="store_true", help=text(language, "help.option_yes"))
    return common


def with_default_command(arguments: list[str]) -> list[str]:
    """不带命令时执行 status(`tightrein`、`tightrein --json`)。"""
    if not arguments or (arguments[0].startswith("-") and arguments[0] not in NOT_A_COMMAND):
        return [DEFAULT_COMMAND, *arguments]
    return arguments


def renamed(arguments: Sequence[str]) -> tuple[str, str] | None:
    """老写法与新写法；不是老写法时为空。"""
    words = [item for item in arguments if not item.startswith("-")][:2]
    for key in (tuple(words[:2]), tuple(words[:1])):
        if key in RENAMED:
            return " ".join(key), RENAMED[key]
    return None


def _language_of(arguments: Sequence[str]) -> str:
    """帮助与解析错误的语言：--lang 给了就用它，否则缺省(此时还不知道是哪个项目)。"""
    for index, item in enumerate(arguments):
        if item == "--lang" and index + 1 < len(arguments):
            return pick_language(arguments[index + 1])
        if item.startswith("--lang="):
            return pick_language(item.split("=", 1)[1])
    return DEFAULT_LANGUAGE


def _object_locks(layout: WorkspaceLayout) -> list[Path]:
    # layout 没有锁目录的属性：从对象锁的路径取目录
    directory = layout.object_lock("_").parent
    return sorted(directory.glob("*.lock")) if directory.is_dir() else []


def _grace(session: Session) -> float:
    workspace = session.opened
    if workspace is None:
        return FALLBACK_GRACE_S
    return recovery.shutdown_grace(workspace.settings)


def _version() -> str:
    try:
        return version(PROG)
    except PackageNotFoundError:
        return "unknown"


def _terminate(signum: int, _frame: FrameType | None) -> None:
    raise Interrupted(signum)
