"""扩展点调用的结果(architecture/10 1.3、2.3、2.4)。

PointResult 有三种情形：output 不为空为成功；failure 不为空为失败；两者都为空为核心默认(没有扩展，或扩展报告
not-applicable)，notes 说明原因。implementation 为数据实际来自的层，调用方据此在运行摘要与 collect 的
stats.extensions 中说明。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint


@dataclass(frozen=True)
class ExtensionFailure:
    code: ExtensionErrorCode
    message: str
    hint: str | None = None
    details: tuple[str, ...] = ()

    def describe(self) -> str:
        text = f"{self.code.value}：{self.message}"
        return text if self.hint is None else f"{text}；{self.hint}"


@dataclass(frozen=True)
class PointResult:
    point: ExtensionPoint
    implementation: ExtensionLayer
    output: Mapping[str, Any] | None = None
    failure: ExtensionFailure | None = None
    notes: tuple[str, ...] = ()
    cached: bool = False

    @property
    def failed(self) -> bool:
        return self.failure is not None

    def stats_entry(self) -> dict[str, Any]:
        """collect 交接文档 stats.extensions 中该扩展点的一项。"""
        return {"implementation": self.implementation.value, "cached": self.cached}
