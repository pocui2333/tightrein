"""本机通知与去重(architecture/01 6.3，architecture/09 3.7)。

- 方式取本机用户配置的 notify.method：macos 时执行 `osascript -e 'display notification "<正文>" with title "<标题>"'`；
  none 时不发，不调用任何外部命令，也不写幂等键。
- 以「事件类型 + 对象 + 日期」为幂等键写入 idempotency_keys，键为 `notify:<事件类型>:<对象>:<日期>`，同一天同一事件
  只通知一次；日期是 Clock 的当前时间在本机时区中的日期，与用户感知的「今天」一致，时区可以注入(测试用固定时区)。
- 通知失败不抛出：幂等键随失败删除，之后可以重试；返回结果带失败原因，由运行摘要写入「异常」一节。
  上一次通知在执行中被中断时键停在进行中，视为已经通知，不重复打扰。
- 标题与正文发出前经过脱敏。外部命令经 run 注入，测试不调用真实的 osascript。
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, tzinfo

from tightrein.config import layers
from tightrein.config.user import NOTIFY_METHODS, NOTIFY_NONE
from tightrein.domain.clock import Clock, local_date
from tightrein.observability.redact import Redactor
from tightrein.store import idempotency
from tightrein.store.idempotency import InProgress

OSASCRIPT = "osascript"
DEFAULT_TITLE = "tightrein"
KEY_PREFIX = "notify"

SENT = "sent"
DUPLICATE = "duplicate"
DISABLED = "disabled"
FAILED = "failed"

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_command(args: Sequence[str], timeout_seconds: float | None = None) -> subprocess.CompletedProcess[str]:
    """执行通知命令；超时缺省取 runtime.observability.notifyTimeoutSeconds 的核心缺省值，组装根按工作区配置绑定。"""
    timeout = float(layers.core_value("runtime.observability.notifyTimeoutSeconds")) if timeout_seconds is None \
        else timeout_seconds
    return subprocess.run(list(args), capture_output=True, text=True, check=False, timeout=timeout)


@dataclass(frozen=True)
class NotifyResult:
    status: str
    key: str | None = None
    reason: str | None = None


class NotificationFailed(Exception):
    """外部命令无法执行或返回非零。"""


def notification_key(event_type: str, subject_id: str, day: date) -> str:
    if not event_type or ":" in event_type:
        raise ValueError(f"事件类型不能为空，也不能含冒号：{event_type!r}")
    if not subject_id:
        raise ValueError("通知对象不能为空")
    return f"{KEY_PREFIX}:{event_type}:{subject_id}:{day.isoformat()}"


def applescript_string(text: str) -> str:
    """AppleScript 字符串字面量：反斜杠与双引号转义。"""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def osascript_command(title: str, text: str) -> list[str]:
    return [OSASCRIPT, "-e", f"display notification {applescript_string(text)} with title {applescript_string(title)}"]


class Notifier:
    """zone 为空时按本机时区取日期。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        method: str,
        clock: Clock,
        redactor: Redactor,
        run: CommandRunner = run_command,
        zone: tzinfo | None = None,
    ) -> None:
        if method not in NOTIFY_METHODS:
            raise ValueError(f"notify.method 只能是 {' 或 '.join(NOTIFY_METHODS)}：{method}")
        self.conn = conn
        self.method = method
        self.clock = clock
        self.redactor = redactor
        self.zone = zone
        self._run = run

    def _send(self, title: str, text: str) -> idempotency.Result:
        try:
            result = self._run(osascript_command(self.redactor.text(title), self.redactor.text(text)))
        except (OSError, subprocess.SubprocessError) as error:
            raise NotificationFailed(f"无法执行 {OSASCRIPT}：{type(error).__name__}: {error}") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or "没有错误输出"
            raise NotificationFailed(f"{OSASCRIPT} 退出码 {result.returncode}：{self.redactor.text(detail)}")
        return {"method": self.method}

    def notify(self, event_type: str, subject_id: str, text: str, title: str = DEFAULT_TITLE) -> NotifyResult:
        if self.method == NOTIFY_NONE:
            return NotifyResult(DISABLED)
        key = notification_key(event_type, subject_id, local_date(self.clock.now(), self.zone))
        try:
            outcome = idempotency.run_once(self.conn, key, lambda: self._send(title, text), self.clock)
        except InProgress:
            return NotifyResult(DUPLICATE, key, "上一次通知在执行中被中断，视为已通知")
        except NotificationFailed as error:
            return NotifyResult(FAILED, key, str(error))
        except sqlite3.Error as error:
            return NotifyResult(FAILED, key, f"幂等键读写失败：{type(error).__name__}: {error}")
        return NotifyResult(DUPLICATE if outcome.skipped else SENT, key)
