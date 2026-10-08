"""退出码与异常到退出码的映射(cli/README.md「退出码」)。

0 成功；1 失败；2 用法错误；3 被拒绝执行(运行锁被占、处于暂停或急停、额度停机)；4 停在人工关卡。
被中断时按 shell 的惯例：Ctrl-C 为 130，SIGTERM、SIGHUP 为 128 + 信号编号(入口把这两个信号转成 Interrupted)。
"""

from __future__ import annotations

import signal

from tightrein.onboard.setup import SetupInvalid
from tightrein.protocol.process import Interrupted
from tightrein.settings.load import MissingSetting, SettingsInvalid
from tightrein.store.locks import Busy

OK = 0
FAILED = 1
USAGE = 2
REFUSED = 3
GATE = 4
SIGNAL_BASE = 128

STATUS_TEXT = {OK: "ok", FAILED: "failed", USAGE: "usage", REFUSED: "refused", GATE: "gate"}


class UsageError(Exception):
    """命令行参数、项目名、对象编号不合法。"""


class Refused(Exception):
    """被拒绝执行：处于暂停或急停、额度停机、项目未就绪。"""


def for_error(error: BaseException) -> int:
    if isinstance(error, (UsageError, SetupInvalid, SettingsInvalid, MissingSetting, ValueError, LookupError)):
        return USAGE
    if isinstance(error, (Refused, Busy)):
        return REFUSED
    return FAILED


def for_interrupt(stopped: BaseException) -> int:
    """Ctrl-C 为 130；入口转换的信号为 128 + 编号(SIGTERM 143、SIGHUP 129)。"""
    if isinstance(stopped, Interrupted) and stopped.args and isinstance(stopped.args[0], int):
        return SIGNAL_BASE + stopped.args[0]
    return SIGNAL_BASE + signal.SIGINT


def status_text(code: int) -> str:
    return STATUS_TEXT.get(code, "interrupted")
