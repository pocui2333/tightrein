"""Signal 实体(architecture/01 2.2)与它的文档形式(data/signal.schema.json)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tightrein.domain.clock import format_iso, parse_iso
from tightrein.domain.enums import Probe, SignalAggregateState, Source


@dataclass(frozen=True)
class Signal:
    id: str
    run_id: str
    source: Source
    probe: Probe
    check: str
    environment: str
    occurred_at: datetime
    release: str | None
    location: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    actor: dict[str, Any] = field(default_factory=dict)
    normalized_message: str | None = None
    fingerprint: str | None = None
    suppressed: bool = False
    aggregate_state: SignalAggregateState = SignalAggregateState.PENDING

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at 必须带时区")

    def ctx(self, *keys: str) -> Any:
        """按路径读取 context 中的值，路径中任一层不存在时返回 None。"""
        value: Any = self.context
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "runId": self.run_id,
            "source": self.source.value,
            "probe": self.probe.value,
            "check": self.check,
            "environment": self.environment,
            "occurredAt": format_iso(self.occurred_at),
            "release": self.release,
            "location": self.location,
            "message": self.message,
            "context": self.context,
            "actor": self.actor,
            "normalizedMessage": self.normalized_message,
            "fingerprint": self.fingerprint,
            "suppressed": self.suppressed,
            "aggregateState": self.aggregate_state.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Signal":
        """聚合字段可以省略，省略时取未聚合的缺省值。"""
        return cls(
            id=data["id"],
            run_id=data["runId"],
            source=Source(data["source"]),
            probe=Probe(data["probe"]),
            check=data["check"],
            environment=data["environment"],
            occurred_at=parse_iso(data["occurredAt"]),
            release=data["release"],
            location=data["location"],
            message=data["message"],
            context=data["context"],
            actor=data["actor"],
            normalized_message=data.get("normalizedMessage"),
            fingerprint=data.get("fingerprint"),
            suppressed=bool(data.get("suppressed", False)),
            aggregate_state=SignalAggregateState(data.get("aggregateState", SignalAggregateState.PENDING.value)),
        )
