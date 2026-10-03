"""Signal 的构造(architecture/04 1.5)。

- 编号为 `S-<ULID>`：时间取 target.clock，随机部分由注入的随机源生成；
- run_id、environment 取目标，suppressed 固定为 false，normalized_message 与 fingerprint 留给 aggregate；
- message 经脱敏后截断到 1000 字符；occurred_at 换算为 UTC 并去掉秒以下的部分；
- context 经脱敏(已按 `<TOKEN>` 处理过的 reproduce 保持原样)，序列化后超过 16 KB 时，从最大的一项起逐项写入
  原始输出目录的 refs/，原位置换成 `<键>Ref`，直到不超过上限。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from tightrein.domain import ids
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import Source
from tightrein.domain.signal import Signal
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.raw import RawDir
from tightrein.sources.common.redact import ProbeRedactor, truncate

ULID_RANDOM_BYTES = 10
MILLISECONDS_PER_SECOND = 1000
REFS_DIR = "refs"
REF_SUFFIX = "Ref"
PRESERVED_KEYS = frozenset({"reproduce"})

RandomBytes = Callable[[int], bytes]


def serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class SignalFactory:
    def __init__(self, target: ProbeTarget, probe: ProbeKind, redactor: ProbeRedactor, *,
                 randomness: RandomBytes = os.urandom) -> None:
        self.target = target
        self.probe = probe
        self.redactor = redactor
        self.randomness = randomness
        self.raw = RawDir(target.raw_dir)

    def new_id(self) -> str:
        now = self.target.clock.now()
        return ids.signal_id(int(now.timestamp() * MILLISECONDS_PER_SECOND), self.randomness(ULID_RANDOM_BYTES))

    def create(
        self, *, source: Source, check: str, location: str, message: str, occurred_at: datetime,
        release: str | None, context: Mapping[str, Any], actor: Mapping[str, Any] | None = None,
    ) -> Signal:
        if not location:
            raise ValueError("信号的 location 不能为空")
        signal_id = self.new_id()
        cleaned = {key: value if key in PRESERVED_KEYS else self.redactor.value(value)
                   for key, value in context.items()}
        return Signal(
            id=signal_id, run_id=self.target.run_id, source=source, probe=self.probe, check=check,
            environment=self.target.environment,
            occurred_at=occurred_at.astimezone(timezone.utc).replace(microsecond=0), release=release,
            location=location, message=truncate(self.redactor.text(message), self.redactor.limits.message_chars),
            context=self.fit(signal_id, cleaned), actor=dict(actor or {}),
        )

    def fit(self, signal_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """把 context 缩到上限(runtime.sources.contextBytes)以内：最大的一项写入 refs/<信号编号>-<键>.json，
        原位置换成 <键>Ref。"""
        result = dict(context)
        while serialized_size(result) > self.redactor.limits.context_bytes:
            movable = [key for key in result if not key.endswith(REF_SUFFIX)]
            if not movable:
                raise ValueError(f"信号 {signal_id} 的 context 只剩引用仍超过上限")
            key = max(movable, key=lambda name: serialized_size(result[name]))
            reference = self.raw.write_json(f"{REFS_DIR}/{signal_id}-{key}.json", result.pop(key))
            result[f"{key}{REF_SUFFIX}"] = reference
        return result
