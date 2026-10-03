import pytest

from tightrein.domain.enums import Complexity, SizeTier
from tightrein.domain.sizing import ComplexityHint, TierLimit, larger, size_tier
from tightrein.domain.sizing import complexity as rate_complexity

LIMITS = {SizeTier.MICRO: TierLimit(1, 30), SizeTier.SMALL: TierLimit(3, 100), SizeTier.MEDIUM: TierLimit(10, 500),
          SizeTier.LARGE: TierLimit(30, 3000)}


@pytest.mark.parametrize("files,lines,expected", [
    (0, 0, SizeTier.MICRO), (1, 30, SizeTier.MICRO), (1, 31, SizeTier.SMALL), (3, 100, SizeTier.SMALL),
    (4, 10, SizeTier.MEDIUM), (10, 500, SizeTier.MEDIUM), (11, 1, SizeTier.LARGE), (30, 3000, SizeTier.LARGE),
    (31, 1, SizeTier.OVERSIZE), (1, 3001, SizeTier.OVERSIZE),
])
def test_size_tier(files, lines, expected):
    assert size_tier(files, lines, LIMITS) is expected


def test_size_tier_rejects_negative():
    with pytest.raises(ValueError):
        size_tier(-1, 0, LIMITS)


def test_larger_only_goes_up():
    assert larger(SizeTier.SMALL, SizeTier.MICRO) is SizeTier.SMALL
    assert larger(SizeTier.SMALL, SizeTier.LARGE) is SizeTier.LARGE


def complexity(hint):
    return rate_complexity(hint, 1)


def test_complexity_high_for_sensitive_or_no_stack():
    assert complexity(ComplexityHint(touches_authorization=True)) is Complexity.HIGH
    assert complexity(ComplexityHint(touches_data_ownership=True)) is Complexity.HIGH
    assert complexity(ComplexityHint(touches_concurrency=True)) is Complexity.HIGH
    assert complexity(ComplexityHint(has_stack=False)) is Complexity.HIGH


def test_complexity_medium_for_multi_file_or_cross_stack():
    assert complexity(ComplexityHint(has_stack=True, estimated_files=3)) is Complexity.MEDIUM
    assert complexity(ComplexityHint(has_stack=True, cross_frontend_backend=True)) is Complexity.MEDIUM


def test_complexity_low_for_single_located_with_stack():
    assert complexity(ComplexityHint(has_stack=True, estimated_files=1)) is Complexity.LOW


def test_complexity_static_problems_count_as_having_stack():
    # 静态问题的位置已经精确到方法，不需要运行时堆栈
    assert complexity(ComplexityHint(location_is_code=True)) is Complexity.LOW


def test_file_limits_come_from_the_caller():
    assert rate_complexity(ComplexityHint(has_stack=True, estimated_files=2), 2) is Complexity.LOW
