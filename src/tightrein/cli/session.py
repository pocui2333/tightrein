"""一条命令的执行环境与结果：解析好的参数、外部依赖、按需打开的工作区，命令交回的 Result。

- 工作区、配置、数据库连接都按需打开(`project list`、`admin install` 不需要)，命令结束时由 main 收尾关闭；
- 会改东西的命令先列出要做什么，问 y/N；`--yes` 跳过。非交互(--json 或标准输入不是终端)又没给 --yes 时拒绝，
  不悄悄当成同意；
- 输出：人读时第一行是结论，随后是明细，最后一行是下一步命令；--json 时标准输出只写一个 JSON 对象。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO

from tightrein.cli import exit_codes
from tightrein.cli.assemble import Externals, Workspace, new_run, open_workspace, resolve_project, runtime
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.text import LANGUAGES, text
from tightrein.protocol.naming import kind_of
from tightrein.protocol.runtime import Runtime
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import runs

DEFAULT_LANGUAGE = "zh"
AGREE = ("y", "yes")


@dataclass
class Result:
    command: str
    code: int = exit_codes.OK
    lines: list[str] = field(default_factory=list)
    data: Any = None  # --json 时的 result
    next: str | None = None  # 推荐的下一条命令，能直接复制执行
    errors: list[dict[str, str | None]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"command": self.command, "status": exit_codes.status_text(self.code), "exitCode": self.code,
                "result": self.data, "next": self.next, "errors": self.errors}


@dataclass
class Session:
    args: argparse.Namespace
    externals: Externals
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    failure: BaseException | None = None  # 命令以什么结束；收尾据此把仍在进行的运行标为中断或失败
    _workspace: Workspace | None = None

    @property
    def command(self) -> str:
        return str(getattr(self.args, "command_name", None) or self.args.command)

    @property
    def json(self) -> bool:
        return bool(getattr(self.args, "json", False))

    @property
    def language(self) -> str:
        """--lang 优先，其次项目的输出语言；文案表没有的语言取英文(protocol/naming.md「中文与英文」)。"""
        chosen = getattr(self.args, "lang", None)
        if chosen is None and self._workspace is not None and self._workspace.settings.project is not None:
            chosen = self._workspace.settings.project.language
        return pick_language(chosen or DEFAULT_LANGUAGE)

    def text(self, key: str, **values: Any) -> str:
        return text(self.language, key, **values)

    # 工作区

    @property
    def project(self) -> str:
        return resolve_project(self.externals, getattr(self.args, "project", None))

    @property
    def workspace(self) -> Workspace:
        if self._workspace is None:
            self._workspace = open_workspace(self.externals, self.project)
        return self._workspace

    @property
    def opened(self) -> Workspace | None:
        return self._workspace

    @property
    def layout(self) -> WorkspaceLayout:
        return self.workspace.layout

    def runtime(self, stage: str) -> Runtime:
        """一次运行的依赖；运行编号按阶段取(同秒已有则顺延)。"""
        return runtime(self.externals, self.workspace, new_run(self.workspace, self.externals, stage))

    def begin(self, stage: str) -> Runtime:
        """命令自己发起、要记进 runs 表的运行(tightrein new 等)：结束时调用 end；中途出错由 main 的收尾标为失败。"""
        current = self.runtime(stage)
        runs.start(current.conn, runs.Run(id=current.run, stage=stage, trigger="manual", status="running",
                                          started_at=current.clock.now()))
        return current

    def end(self, current: Runtime) -> None:
        runs.finish(current.conn, current.run, "done", current.clock)

    # 确认

    def confirm(self, actions: Sequence[str]) -> bool:
        """列出要做的事，问 y/N。"""
        if getattr(self.args, "yes", False):
            return True
        if self.json or not self.externals.stdin_is_tty():
            raise UsageError(self.text("cli.confirm_needs_yes"))
        self.stdout.write(self.text("cli.confirm_title") + "\n")
        self.stdout.writelines(f"  - {action}\n" for action in actions)
        self.stdout.write(self.text("cli.confirm_prompt"))
        self.stdout.flush()
        return self.stdin.readline().strip().lower() in AGREE

    def declined(self) -> Result:
        return Result(self.command, exit_codes.OK, [self.text("cli.declined")])


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser], name: str, *,
                common: argparse.ArgumentParser, language: str, help_key: str,
                handler: Callable[[Session], Result] | None = None) -> argparse.ArgumentParser:
    """加一个命令(或分组下的子命令)：挂上全局选项，帮助取文案表；command_name 用于输出中的命令名。"""
    parser = commands.add_parser(name, parents=[common], help=text(language, help_key),
                                 description=text(language, help_key))
    if handler is not None:
        parser.set_defaults(handler=handler, command_name=parser.prog.partition(" ")[2])
    return parser


def add_group(commands: argparse._SubParsersAction[argparse.ArgumentParser], name: str, *, language: str,
              help_key: str) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    """分组(project、problem、retro、knowledge、admin)：子命令必填。"""
    group = commands.add_parser(name, help=text(language, help_key), description=text(language, help_key))
    return group.add_subparsers(dest="subcommand", required=True, parser_class=type(group), metavar="<subcommand>")


def subject_id(value: str) -> str:
    """编号直接写 `19`、`0019` 或 `P-0003`，不加 `#`；数字补足四位。不认识的编号报用法错误。"""
    stripped = value.strip().lstrip("#")
    subject = stripped.zfill(4) if stripped.isdigit() else stripped
    try:
        kind_of(subject)
    except ValueError as failure:
        raise UsageError(str(failure)) from failure
    return subject


def pick_language(language: str) -> str:
    code = language.replace("_", "-").split("-")[0].lower()
    return code if code in LANGUAGES else "en"


def emit(result: Result, json_mode: bool, stdout: TextIO, language: str) -> None:
    if json_mode:
        stdout.write(json.dumps(result.to_json(), ensure_ascii=False, default=str) + "\n")
        return
    lines = list(result.lines)
    for error in result.errors:
        lines.append(text(language, "cli.error", kind=error["type"], message=error["message"]))
        if error.get("hint"):
            lines.append(text(language, "cli.hint", hint=error["hint"]))
    if result.next:
        lines.append(text(language, "cli.next", command=result.next))
    stdout.write("\n".join(lines) + ("\n" if lines else ""))


def error(kind: str, message: str, hint: str | None = None) -> dict[str, str | None]:
    return {"type": kind, "message": message, "hint": hint}
