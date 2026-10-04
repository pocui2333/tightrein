"""退出码(architecture/09 4.5)与各层异常到退出码的映射。未映射的异常一律为 1，输出中给出事件日志路径。

kb 命令同样使用这张表：查询串不合法与编号不存在为 2，数据库不可用(先执行 init)为 3，知识文件格式错误为 1。
命令被中断时按 shell 的惯例：Ctrl+C 为 130，SIGTERM、SIGHUP 为 128 + 信号编号(入口把这两个信号转成 Terminated)。
"""

from __future__ import annotations

import sqlite3

from tightrein.config.project import ConfigError, MissingSetting
from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.enums import HandoffStatus
from tightrein.guards.report import GuardBlocked
from tightrein.retrieval.errors import EntryNotFound, IndexUnavailable, InvalidQuery
from tightrein.store.locks import FileLockBusy, LockHeld
from tightrein.vcs.executor import OperationStateError

OK = 0
FAILED = 1
USAGE = 2
PRECONDITION = 3
GATE = 4
LIMIT = 5
GUARD = 6
LOCKED = 7
OUTPUT = 8
SIGNAL_BASE = 128
INTERRUPTED = SIGNAL_BASE + 2  # SIGINT

STATUS_TEXT = {OK: "ok", PRECONDITION: "blocked", GATE: "blocked", LOCKED: "blocked"}


class Terminated(BaseException):
    """进程收到 SIGTERM 或 SIGHUP(cli/main.entry 安装的处理函数抛出)；与 KeyboardInterrupt 同级，不会被 except Exception 吞掉。"""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"收到信号 {signum}")


def for_interrupt(stopped: BaseException) -> int:
    return SIGNAL_BASE + stopped.signum if isinstance(stopped, Terminated) else INTERRUPTED


class UsageError(Exception):
    """命令行参数、工作区、对象编号或选择器不合法(退出码 2)。"""


def for_error(error: BaseException) -> int:
    if isinstance(error, (UsageError, ConfigError, MissingSetting, ValueError, LookupError, InvalidQuery,
                          EntryNotFound)):
        return USAGE
    if isinstance(error, (OperationStateError, IndexUnavailable, sqlite3.Error)):
        return PRECONDITION
    if isinstance(error, GuardBlocked):
        return GUARD
    if isinstance(error, (LockHeld, FileLockBusy)):
        return LOCKED
    if isinstance(error, SchemaValidationError):
        return OUTPUT
    return FAILED


def for_status(status: HandoffStatus | None, operation: str | None = None) -> int:
    """模块结果的退出码：blocked 且生成了待确认操作为 4，其余 blocked 为前置条件不满足(3)。"""
    if status is HandoffStatus.FAILED:
        return FAILED
    if status is HandoffStatus.BLOCKED:
        return GATE if operation else PRECONDITION
    return OK


def status_text(code: int) -> str:
    return STATUS_TEXT.get(code, "failed")
