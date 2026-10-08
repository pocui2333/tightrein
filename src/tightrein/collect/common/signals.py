"""信号(44c)：各来源交给去重的统一格式，与生成信号的共用部分。

- 编号 `S-<ULID>`：48 位毫秒时间加 80 位随机数，Crockford Base32；时间取注入的时钟，随机源可注入(测试可复现)；
- message 先脱敏再截断：顺序反了，截断可能切开密钥，脱敏规则就认不出了；
- occurred_at 换成 UTC 并去掉秒以下；location 给出时不能是空串；
- evidence 逐项脱敏(reproduce 例外：产出它的来源已把凭据换成 `<TOKEN>`，再按文本规则处理会把
  `Authorization: Bearer <TOKEN>` 整个换掉)；序列化后超过 evidenceBytes 时，从最大的一项起写到原始输出的
  `refs/<信号编号>-<键>.json`，
  原处改成 `<键>Ref`，直到不超过上限；只剩引用还超限就报错；
- commit 取发生时间之前最近一次成功部署的 commit(部署时间缺失时用检测时间，同一时间按 commit 排序，结果稳定)；
  回归与解决的判定都依赖它。部署记录由发布阶段(release/deploy.py)写进 state 表的 DEPLOYMENTS_KEY。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tightrein.protocol.naming import Clock, format_iso, parse_iso
from tightrein.protocol.raw import RawDir, raw_dir
from tightrein.protocol.security import Redactor
from tightrein.settings.load import Settings
from tightrein.store.tables import state

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

ULID_RANDOM_BYTES = 10
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MILLISECONDS_PER_SECOND = 1000
REFS_DIR = "refs"
REF_SUFFIX = "Ref"
DEPLOYMENTS_KEY = "release.deploy.deployments"  # [{commit, status, deployedAt, detectedAt}]
DEPLOY_SUCCEEDED = "succeeded"
SEVERITY_HINTS = ("P0", "P1", "P2", "P3")
VERBATIM_EVIDENCE = frozenset({"reproduce"})

RandomBytes = Callable[[int], bytes]
ReleaseAt = Callable[[datetime], str | None]


@dataclass(frozen=True)
class Signal:
    id: str  # S-<ULID>
    run: str
    source: str  # 控制键：collect.platform_errors
    check_type: str  # error、latency、error_rate、alert、server_error、static、probe、incidental:<类别>
    location: str | None  # path:line、路由(GET /api/x)、或 None
    symbol: str | None  # 函数或方法名
    message: str
    evidence: dict[str, Any]  # 已脱敏；交给评估
    occurred_at: str  # ISO UTC
    commit: str | None  # 发生时的版本(部署的 commit)
    environment: str | None
    severity_hint: str | None  # P0..P3
    group_key: str | None  # 平台分组编号等稳定键，有则作为指纹依据
    deterministic: bool  # 确定性来源：不走「偶发先观察」
    verified: bool  # 已由模型逐条取证成立(静态巡检)
    reproducible: bool  # 去重时已重放确认(API 模糊测试)

    def to_json(self) -> dict[str, Any]:
        return {_camel(key): value for key, value in asdict(self).items()}


@dataclass(frozen=True)
class SignalLimits:
    message_chars: int
    evidence_bytes: int
    excerpt_chars: int  # 日志、响应原文摘录

    @classmethod
    def from_settings(cls, settings: Settings, source: str) -> SignalLimits:
        """取值在 settings 的 controls.collect(各来源可在自己的控制键下覆盖)。"""
        return cls(int(settings.control(source, "messageChars")), int(settings.control(source, "evidenceBytes")),
                   int(settings.control(source, "excerptChars")))


@dataclass(frozen=True)
class Deployment:
    commit: str
    status: str
    deployed_at: datetime | None
    detected_at: datetime

    @property
    def at(self) -> datetime:
        return self.deployed_at or self.detected_at


class SignalFactory:
    def __init__(self, *, run: str, source: str, clock: Clock, redactor: Redactor, raw: RawDir, limits: SignalLimits,
                 randomness: RandomBytes = os.urandom) -> None:
        self.run = run
        self.source = source
        self.clock = clock
        self.redactor = redactor
        self.raw = raw
        self.limits = limits
        self.randomness = randomness

    def new_id(self) -> str:
        now = self.clock.now()
        return signal_id(int(now.timestamp() * MILLISECONDS_PER_SECOND), self.randomness(ULID_RANDOM_BYTES))

    def create(self, *, check_type: str, location: str | None, message: str, occurred_at: datetime,
               commit: str | None, evidence: Mapping[str, Any], symbol: str | None = None,
               environment: str | None = None, severity_hint: str | None = None, group_key: str | None = None,
               deterministic: bool = False, verified: bool = False, reproducible: bool = False) -> Signal:
        if location is not None and not location.strip():
            raise ValueError("信号的 location 不能是空串(没有位置时写 None)")
        if severity_hint is not None and severity_hint not in SEVERITY_HINTS:
            raise ValueError(f"severity_hint 只能是 P0..P3：{severity_hint}")
        signal = self.new_id()
        return Signal(
            id=signal, run=self.run, source=self.source, check_type=check_type, location=location, symbol=symbol,
            message=truncate(self.redactor.text(message), self.limits.message_chars),
            evidence=self.fit(signal, self._redacted(evidence)),
            occurred_at=format_iso(occurred_at.astimezone(UTC)), commit=commit, environment=environment,
            severity_hint=severity_hint, group_key=group_key, deterministic=deterministic, verified=verified,
            reproducible=reproducible,
        )

    def excerpt(self, text: str) -> str:
        """原文摘录：先脱敏再截断。"""
        return truncate(self.redactor.text(text), self.limits.excerpt_chars)

    def _redacted(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value if key in VERBATIM_EVIDENCE else self.redactor.mapping({key: value})[key]
                for key, value in evidence.items()}

    def fit(self, signal: str, evidence: dict[str, Any]) -> dict[str, Any]:
        result = dict(evidence)
        while serialized_size(result) > self.limits.evidence_bytes:
            movable = [key for key in result if not key.endswith(REF_SUFFIX)]
            if not movable:
                raise ValueError(f"信号 {signal} 的证据只剩引用仍超过上限 {self.limits.evidence_bytes} 字节")
            key = max(movable, key=lambda name: serialized_size(result[name]))
            result[f"{key}{REF_SUFFIX}"] = self.raw.write_json(f"{REFS_DIR}/{signal}-{key}.json", result.pop(key))
        return result


def factory_for(runtime: Runtime, source: str, *, randomness: RandomBytes = os.urandom) -> SignalFactory:
    """一个来源本次运行的信号工厂：原始输出放在运行目录的 `<序号>-<来源>-raw/`。"""
    return SignalFactory(run=runtime.run, source=source, clock=runtime.clock, redactor=runtime.redactor,
                         raw=RawDir(raw_dir(runtime.workspace, runtime.run, source)),
                         limits=SignalLimits.from_settings(runtime.settings, source), randomness=randomness)


def signal_id(timestamp_ms: int, randomness: bytes) -> str:
    if len(randomness) != ULID_RANDOM_BYTES:
        raise ValueError("ULID 需要 10 字节随机数")
    if not 0 <= timestamp_ms < 2**48:
        raise ValueError("时间戳超出 ULID 范围")
    value = (timestamp_ms << 80) | int.from_bytes(randomness, "big")
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "S-" + "".join(reversed(chars))


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def release_at(deployments: Sequence[Deployment], at: datetime) -> str | None:
    found = [item for item in _succeeded(deployments) if item.at <= at]
    return found[-1].commit if found else None


def latest_release(deployments: Sequence[Deployment]) -> str | None:
    found = _succeeded(deployments)
    return found[-1].commit if found else None


def deployments(conn: sqlite3.Connection) -> list[Deployment]:
    """发布阶段记下的部署；没有记录时为空(信号的 commit 为 None)。"""
    found = state.get(conn, DEPLOYMENTS_KEY) or []
    return [Deployment(item["commit"], item["status"],
                       parse_iso(item["deployedAt"]) if item.get("deployedAt") else None, parse_iso(item["detectedAt"]))
            for item in found]


def releases(conn: sqlite3.Connection) -> ReleaseAt:
    """一次读出部署记录，之后每条信号只在内存中查(不在循环里逐条查库)。"""
    known = _succeeded(deployments(conn))
    return lambda at: release_at(known, at)


def _succeeded(found: Sequence[Deployment]) -> list[Deployment]:
    return sorted((item for item in found if item.status == DEPLOY_SUCCEEDED), key=lambda item: (item.at, item.commit))


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)
