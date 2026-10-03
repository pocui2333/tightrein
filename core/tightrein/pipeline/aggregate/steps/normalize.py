"""第 1 步 规范化(design 2.5)：内置默认规则加 normalize.yaml 的项目规则，结果写入信号的 normalized_message。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from tightrein.domain.normalize import Rule, normalize
from tightrein.domain.signal import Signal
from tightrein.pipeline.aggregate.changeset import ChangeSet


def apply(changeset: ChangeSet, signals: Sequence[Signal], rules: tuple[Rule, ...]) -> list[Signal]:
    result = []
    for signal in signals:
        normalized = replace(signal, normalized_message=normalize(signal.message, rules))
        changeset.put_signal(normalized)
        result.append(normalized)
    return result
