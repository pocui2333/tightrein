"""第 2 步 抑制(design 2.9)：命中未到期抑制规则的信号标记为已抑制并结束处理。返回仍待处理的信号。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from tightrein.domain.enums import SignalAggregateState
from tightrein.domain.signal import Signal
from tightrein.domain.suppression import SuppressionRule, match
from tightrein.pipeline.aggregate.changeset import ChangeSet


def apply(changeset: ChangeSet, signals: Sequence[Signal], rules: Sequence[SuppressionRule],
          now: datetime) -> list[Signal]:
    remaining = []
    for signal in signals:
        if match(signal, rules, now) is None:
            remaining.append(signal)
            continue
        changeset.put_signal(replace(signal, suppressed=True, aggregate_state=SignalAggregateState.DONE))
        changeset.current.suppressed += 1
    return remaining
