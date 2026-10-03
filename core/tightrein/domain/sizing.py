"""规模档与任务复杂度(redesign/03-triage.md、05-fix.md；design 13.3)。门槛由调用方按 thresholds.tiers 与
thresholds.triage.complexityFiles 传入。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tightrein.domain.enums import Complexity, SizeTier

TIER_ORDER = (SizeTier.MICRO, SizeTier.SMALL, SizeTier.MEDIUM, SizeTier.LARGE, SizeTier.OVERSIZE)


@dataclass(frozen=True)
class TierLimit:
    max_files: int
    max_lines: int


def size_tier(files: int, lines: int, limits: Mapping[SizeTier, TierLimit]) -> SizeTier:
    """文件数与行数(都不含测试文件)都不超过某档上限时取该档，按微、小、中、大依次判断；都超出时为超限。"""
    if files < 0 or lines < 0:
        raise ValueError(f"改动量不能为负：{files} 个文件、{lines} 行")
    for tier in TIER_ORDER[:-1]:
        limit = limits[tier]
        if files <= limit.max_files and lines <= limit.max_lines:
            return tier
    return SizeTier.OVERSIZE


def larger(first: SizeTier, second: SizeTier) -> SizeTier:
    """两档中较大的一档；重评规模档只升不降。"""
    return max(first, second, key=TIER_ORDER.index)


@dataclass(frozen=True)
class ComplexityHint:
    """分诊或修复开始前能拿到的线索。"""

    touches_authorization: bool = False
    touches_data_ownership: bool = False
    touches_concurrency: bool = False
    has_stack: bool = False
    location_is_code: bool = False
    cross_frontend_backend: bool = False
    estimated_files: int = 1


def complexity(hint: ComplexityHint, low_files: int) -> Complexity:
    """高：涉及权限、数据归属或并发，或没有调用栈也不是代码位置；中：预估改动超过 low_files 个文件或跨前后端；其余为低。"""
    if hint.touches_authorization or hint.touches_data_ownership or hint.touches_concurrency:
        return Complexity.HIGH
    if not (hint.has_stack or hint.location_is_code):
        return Complexity.HIGH
    if hint.estimated_files > low_files or hint.cross_frontend_backend:
        return Complexity.MEDIUM
    return Complexity.LOW
