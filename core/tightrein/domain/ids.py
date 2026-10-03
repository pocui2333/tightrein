"""编号的生成、解析与校验(architecture/01 1.1)。序号本身由 store 的 sequences 表分配。"""

from __future__ import annotations

import re
from datetime import date, datetime

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import KnowledgeType, Probe, RunStage

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_KNOWLEDGE_PREFIXES = "|".join(member.prefix for member in KnowledgeType)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("run", re.compile(r"^R-\d{8}-\d{6}-[a-z]+(-[a-z0-9-]+)?$")),
    ("signal", re.compile(r"^S-[0-9A-HJKMNP-TV-Z]{26}$")),
    ("problem", re.compile(r"^P-\d{4,}$")),
    ("issue", re.compile(r"^\d{4,}$")),
    ("operation", re.compile(r"^OP-\d{4,}$")),
    ("suggestion", re.compile(r"^LS-\d{4,}$")),
    ("eval", re.compile(r"^EV-\d{8}-\d{6}$")),
    ("eval-case", re.compile(r"^E-\d{4,}$")),
    ("knowledge", re.compile(rf"^({_KNOWLEDGE_PREFIXES})-\d{{4,}}$")),
)


def _timestamp(now: datetime) -> str:
    return format_iso(now).replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")


def _sequence(prefix: str, number: int) -> str:
    if number < 1:
        raise ValueError(f"序号必须大于 0：{number}")
    return f"{prefix}{number:04d}"


def run_id(now: datetime, stage: RunStage, probe: Probe | None = None) -> str:
    if stage is RunStage.COLLECT and probe is None:
        raise ValueError("collect 的运行编号必须带探针")
    if stage is not RunStage.COLLECT and probe is not None:
        raise ValueError("只有 collect 的运行编号带探针")
    suffix = f"collect-{probe.value}" if probe is not None else stage.value
    return f"R-{_timestamp(now)}-{suffix}"


def signal_id(timestamp_ms: int, randomness: bytes) -> str:
    """ULID：48 位毫秒时间戳加 80 位随机数，Crockford Base32 编码。"""
    if len(randomness) != 10:
        raise ValueError("ULID 需要 10 字节随机数")
    if not 0 <= timestamp_ms < 2**48:
        raise ValueError("时间戳超出 ULID 范围")
    value = (timestamp_ms << 80) | int.from_bytes(randomness, "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "S-" + "".join(reversed(chars))


def problem_id(number: int) -> str:
    return _sequence("P-", number)


def issue_id(number: int) -> str:
    return _sequence("", number)


def operation_id(number: int) -> str:
    return _sequence("OP-", number)


def suggestion_id(number: int) -> str:
    return _sequence("LS-", number)


def eval_case_id(number: int) -> str:
    return _sequence("E-", number)


def eval_id(now: datetime) -> str:
    return f"EV-{_timestamp(now)}"


def knowledge_id(kind: KnowledgeType, number: int) -> str:
    return _sequence(f"{kind.prefix}-", number)


def knowledge_sequence(kind: KnowledgeType) -> str:
    return f"knowledge-{kind.prefix}"


def handoff_id(stage: str, subject_id: str) -> str:
    return f"{stage}-{subject_id}"


def verify_handoff_id(phase: str, issue: str) -> str:
    return f"verify-{phase}-{issue}"


def weekly_handoff_id(day: date) -> str:
    return f"learn-weekly-{day.isoformat()}"


def kind_of(value: str) -> str:
    for kind, pattern in _PATTERNS:
        if pattern.match(value):
            return kind
    raise ValueError(f"无法识别的编号：{value}")


def parse_sequence(value: str) -> int:
    match = re.search(r"(\d{4,})$", value)
    if match is None or kind_of(value) in {"run", "signal", "eval"}:
        raise ValueError(f"不是序号型编号：{value}")
    return int(match.group(1))
