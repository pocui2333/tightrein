"""命名、编号与时间(protocol/naming.md)。

时间一律经注入的 Clock 取得，存储为 ISO 8601 UTC、去微秒、`Z` 结尾；文件名中写成 `20261007T093000Z`。
时长一律写成数字加单位(`30s`、`20m`、`2h`、`90d`)，程序中统一换算成秒。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Protocol

STAGES = ("collect", "assess", "implement", "release", "retro")

# 文件名序号：十位是阶段，个位是阶段内顺序；00 为对象共用，90 为给人看的文档
STEP_SEQUENCE: dict[str, int] = {
    "collect.project_probes": 11,
    "collect.platform_errors": 12,
    "collect.access_log": 13,
    "collect.alerts": 14,
    "collect.api_fuzz": 15,
    "collect.static": 16,
    "collect.incidental": 17,
    "collect.dedup": 19,
    "assess.triage": 21,
    "assess.issue": 22,
    "implement.prepare": 31,
    "implement.locate": 32,
    "implement.design": 33,
    "implement.approve": 34,
    "implement.code": 35,
    "implement.check": 36,
    "implement.review": 37,
    "implement.deliver": 38,
    "release.pr": 41,
    "release.ci": 42,
    "release.merge": 43,
    "release.deploy": 44,
    "release.accept": 45,
    "release.cleanup": 46,
    "retro.detect": 51,
    "retro.idea": 52,
    "knowledge.curate": 53,
}

CONTENT_WORDS = frozenset(
    {"handoff", "prompt", "raw", "started", "diff", "log", "evidence", "notes", "body", "pending", "failure", "deliver"}
)
HUMAN_DOCUMENTS = ("pending", "failure", "deliver")

_CONTROL_KEY = re.compile(r"^[a-z]+(\.[a-z][a-z0-9_]*){0,3}$")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d)$")
_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
_SEGMENT_FORBIDDEN = re.compile(r"[/\\\0]")
_RUN_ID = re.compile(r"^R-\d{8}T\d{6}Z-[a-z]+$")
_ISSUE_ID = re.compile(r"^\d{4,}$")
_PROBLEM_ID = re.compile(r"^P-\d{4,}$")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class FixedClock:
    """测试与 --now 用的固定时钟。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FixedClock 需要带时区的时间")
        self._value = value.astimezone(UTC)

    def now(self) -> datetime:
        return self._value

    def advance(self, delta: timedelta) -> None:
        self._value = self._value + delta


@dataclass(frozen=True)
class FileName:
    """运行中产生的文件名：`<序号>-<阶段>.<模块>[.<小步骤>][.r<轮>]-<内容>.<扩展名>`。"""

    point: str
    content: str
    extension: str
    round: int | None = None
    sequence: int | None = None

    def render(self) -> str:
        if self.content not in CONTENT_WORDS:
            raise ValueError(f"内容词不在词表中：{self.content}")
        sequence = self.sequence if self.sequence is not None else step_sequence(self.point)
        suffix = f".r{self.round}" if self.round is not None else ""
        return f"{sequence:02d}-{self.point}{suffix}-{self.content}.{self.extension}"


def format_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("format_iso 需要带时区的时间")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    value = datetime.fromisoformat(text)  # Python 3.11 起直接认 `Z`
    if value.tzinfo is None:
        raise ValueError(f"时间缺少时区：{text}")
    return value.astimezone(UTC)


def compact_time(value: datetime) -> str:
    """文件名与运行编号中的时间：20261007T093000Z。"""
    return format_iso(value).replace("-", "").replace(":", "")


def local_date(value: datetime, zone: tzinfo | None = None) -> date:
    """「今天」按本机时区(或给定时区)的日期。"""
    if value.tzinfo is None:
        raise ValueError("local_date 需要带时区的时间")
    return (value.astimezone() if zone is None else value.astimezone(zone)).date()


def parse_duration(text: str) -> float:
    """`30s`、`20m`、`2h`、`90d`、`500ms` → 秒。"""
    match = _DURATION.match(text.strip())
    if match is None:
        raise ValueError(f"时长要写成数字加单位(ms、s、m、h、d)：{text!r}")
    return float(match.group(1)) * _DURATION_UNITS[match.group(2)]


def format_duration(seconds: float) -> str:
    """显示用：`42m`、`1h18m`、`3d`、`12s`。"""
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{total // 86400}d"


def format_count(value: int) -> str:
    """显示用：`120k`、`1.2M`。"""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    return str(value)


def check_control_key(key: str) -> str:
    """控制键「阶段.模块.小步骤」：小写英文，模块名内部可用下划线。"""
    if not _CONTROL_KEY.match(key) or key.split(".")[0] not in STAGES + ("protocol", "agents", "knowledge", "onboard"):
        raise ValueError(f"控制键不合规：{key!r}")
    return key


def key_chain(key: str) -> list[str]:
    """继承顺序(由近到远)：`implement.code` → [`implement.code`, `implement`, `*`]。"""
    parts = check_control_key(key).split(".")
    return [".".join(parts[:size]) for size in range(len(parts), 0, -1)] + ["*"]


def step_sequence(point: str) -> int:
    for size in range(point.count(".") + 1, 0, -1):
        prefix = ".".join(point.split(".")[:size])
        if prefix in STEP_SEQUENCE:
            return STEP_SEQUENCE[prefix]
    raise ValueError(f"没有登记文件序号的步骤：{point}")


def segment(value: str) -> str:
    """路径中的一段：拒绝 `/`、`\\`、`\\0` 与 `.`、`..`，防止外部输入把文件写到预期目录之外。"""
    if not value or value in {".", ".."} or _SEGMENT_FORBIDDEN.search(value):
        raise ValueError(f"不能作为路径的一段：{value!r}")
    return value


def run_id(now: datetime, stage: str) -> str:
    if stage not in STAGES and stage != "run":
        raise ValueError(f"未知的阶段：{stage}")
    return f"R-{compact_time(now)}-{stage}"


def issue_id(number: int) -> str:
    return _sequence("", number)


def problem_id(number: int) -> str:
    return _sequence("P-", number)


def retro_id(number: int) -> str:
    return _sequence("", number)


def kind_of(value: str) -> str:
    """编号的种类：run、issue、problem。问题与 Issue 编号不重叠，命令行据此自动识别。"""
    if _RUN_ID.match(value):
        return "run"
    if _PROBLEM_ID.match(value):
        return "problem"
    if _ISSUE_ID.match(value):
        return "issue"
    raise ValueError(f"无法识别的编号：{value}")


def run_started(value: str) -> datetime:
    """运行编号中的开始时间(保留期清理据此判断年龄，不依赖文件修改时间)。"""
    if not _RUN_ID.match(value):
        raise ValueError(f"不是运行编号：{value}")
    stamp = value.split("-")[1]
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _sequence(prefix: str, number: int) -> str:
    if number < 1:
        raise ValueError(f"序号必须大于 0：{number}")
    return f"{prefix}{number:04d}"
