"""各工具适配器的协议与共用的解析(agents/README.md「兜底规则」，各工具的写法见 agents/tools/<工具>.md)。

适配器只负责命令行写法与输出格式，不做业务判断：
- build 生成命令(参数数组，不经 shell)；
- parse_line 读一行输出，给程序兜底用：数 tool-call、取本行的用量、读额度信号、取要执行的命令与各工具调用的参数；
- parse 从整个输出取会话、最终文本、结构化结果、用量与结束状态，并按该工具的报错识别额度用完、认证失败、被拒绝。
临时错误不在这里识别：片段表在 settings 的 limits.transientPatterns，由 agents/call.py 统一判断。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tightrein.agents.params import CallParams, Model
from tightrein.agents.result import CallStatus, RateLimit
from tightrein.protocol.handoff import Tokens
from tightrein.protocol.process import Command

FINALIZE_PROMPT = "会话已结束。请按给定的 schema 输出本次会话的结构化结果，只输出一个 JSON 对象。"
NO_RESULT = "输出中没有 result 事件"

_CLOCK_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)", re.IGNORECASE)
_MONTH_DAY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.IGNORECASE)
_ZONE = re.compile(r"\(([A-Za-z_]+(?:/[A-Za-z_+\-]+)+|UTC)\)")
_COMPACT_DURATION = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_WORD_DURATION = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


class CallConfigError(Exception):
    """编程或配置错误：未知工具、没有回放录制集、工具不支持的参数组合(联网任务交给 codex)。只有这类错误才抛出。"""


@dataclass(frozen=True)
class Resume:
    """续接已有会话：note 为这一轮的输入；finalize 为真时是「补要结构化结果」的收尾调用。"""

    session_id: str
    note: str
    finalize: bool = False


@dataclass(frozen=True)
class LineEvent:
    tool_calls: int = 0
    commands: tuple[str, ...] = ()  # 要执行的 shell 命令(工具自己不逐条放行时由程序检查)
    tool_inputs: tuple[tuple[str, Any], ...] = ()  # (工具名, 参数)：程序据此检查是否读了隐藏目录或凭据文件
    tokens: Tokens | None = None
    usage_id: str | None = None  # 同一次模型响应分多行输出时用量会重复出现，按它去重
    rate_limit: RateLimit | None = None


NOTHING = LineEvent()


@dataclass
class Parsed:
    """status 只会是 OK、TURN_LIMIT、BUDGET_LIMIT、QUOTA_EXHAUSTED、AUTH_FAILED、REFUSED、FAILED。"""

    status: CallStatus
    session_id: str | None = None
    text: str | None = None
    structured: dict[str, Any] | None = None
    tokens: Tokens = field(default_factory=Tokens)
    cost_usd: float | None = None
    turns: int | None = None
    error: str | None = None
    rate_limits: list[RateLimit] = field(default_factory=list)


class Adapter(Protocol):
    name: str
    native_turns: bool  # 工具自己保证轮数上限；否则程序数 tool-call 兜底
    checks_commands: bool  # 工具自己逐条放行命令；否则程序按白名单检查每条命令
    env_names: tuple[str, ...]  # 除 protocol.security.ENV_ALLOW 外该工具运行必需的环境变量

    def env_values(self, params: CallParams) -> dict[str, str]: ...

    def build(self, params: CallParams, model: Model, *, executable: str, env: dict[str, str],
              schema: dict[str, Any] | None, scratch: Path, resume: Resume | None) -> Command: ...

    def parse_line(self, line: str) -> LineEvent: ...

    def parse(self, stdout: str, stderr: str, exit_code: int | None, now: datetime) -> Parsed: ...


def load_line(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def json_records(stdout: str) -> list[dict[str, Any]]:
    """逐行解析的 JSON 对象；整个输出是一个跨多行的 JSON 对象(--output-format json)时按一个对象解析，不逐行误判。"""
    lines = [line for line in stdout.splitlines() if line.strip()]
    parsed = [load_line(line) for line in lines]
    if lines and any(item is None for item in parsed):
        whole = load_line(stdout)
        if whole is not None:
            return [whole]
    return [item for item in parsed if item is not None]


def text_of(content: Any) -> str:
    """消息内容可能是字符串，也可能是 `{type: text, text}` 等片段的列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def read_dirs(params: CallParams) -> list[str]:
    seen: list[str] = []
    for path in params.read_paths:
        if str(path) not in seen:
            seen.append(str(path))
    return seen


def write_schema(schema: dict[str, Any], scratch: Path) -> Path:
    """只接受 schema 文件路径的工具(agy、codex)：写到本次调用的临时目录。"""
    path = scratch / "schema.json"
    path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    return path


def joined(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def resets_at_clock(text: str, now: datetime) -> datetime | None:
    """「resets 3:45pm」「resets Oct 9, 3pm (Asia/Tokyo)」：claude 按本机(或括号中的)时区写重置时刻；只有时刻时取
    now 之后最近的一次。认不出时返回 None，由 resources.Quota 按 unknownResetWait 处理。"""
    clock = _CLOCK_TIME.search(text)
    if clock is None:
        return None
    zone = _zone(text)
    local_now = now.astimezone(zone)
    hour = int(clock.group(1)) % 12 + (12 if clock.group(3).lower().startswith("p") else 0)
    minute = int(clock.group(2) or 0)
    day = _MONTH_DAY.search(text)
    if day is None:
        moment = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return (moment if moment > local_now else moment + timedelta(days=1)).astimezone(UTC)
    month = _MONTHS.index(day.group(1).lower()[:3]) + 1
    moment = local_now.replace(month=month, day=int(day.group(2)), hour=hour, minute=minute, second=0, microsecond=0)
    if moment < local_now - timedelta(days=1):
        moment = moment.replace(year=moment.year + 1)
    return moment.astimezone(UTC)


def resets_after(text: str, now: datetime) -> datetime | None:
    """「143h57m55s」或「2 days 3 hours」这类时长 → now 加上它；认不出时返回 None。"""
    compact = _COMPACT_DURATION.match(text.strip())
    if compact is not None and any(compact.groups()):
        units = zip(("d", "h", "m", "s"), compact.groups())
        return now + timedelta(seconds=sum(int(value) * _UNIT_SECONDS[unit] for unit, value in units if value))
    seconds = sum(int(number) * _UNIT_SECONDS[unit[0].lower()] for number, unit in _WORD_DURATION.findall(text))
    return now + timedelta(seconds=seconds) if seconds else None


def window_for(resets: datetime | None, now: datetime) -> str:
    """报错没写是哪种额度时按重置时间推断：5 小时以内为 5 小时窗口，更久的为每周额度。"""
    if resets is not None and resets - now > timedelta(hours=5):
        return "weekly"
    return "five_hour"


def quota_limit(tool: str, window: str, resets: datetime | None) -> RateLimit:
    return RateLimit(tool, window, "rejected", 1.0, None if resets is None else iso(resets))


def _zone(text: str) -> Any:
    named = _ZONE.search(text)
    if named is not None:
        try:
            return ZoneInfo(named.group(1))
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo
