from dataclasses import replace
from datetime import date

import pytest

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.domain.knowledge import KnowledgeEntry

ENTRY = KnowledgeEntry(
    id="DP-0012", type=KnowledgeType.DEFECT_PATTERN, title="查询接口缺少数据归属校验",
    summary="按编号查询时未校验公司归属", status=KnowledgeStatus.ACTIVE, updated=date(2026, 9, 1),
    path="knowledge/defect-pattern/DP-0012.md", tags=("authorization",), review_by=date(2026, 12, 1),
)


def test_valid_entry():
    assert ENTRY.hits == 0
    assert ENTRY.last_hit_at is None


def test_id_prefix_must_match_type():
    with pytest.raises(ValueError):
        replace(ENTRY, id="TO-0012")
    with pytest.raises(ValueError):
        replace(ENTRY, id="P-0012")


def test_superseded_requires_successor():
    replace(ENTRY, status=KnowledgeStatus.SUPERSEDED, superseded_by="DP-0020")
    with pytest.raises(ValueError):
        replace(ENTRY, status=KnowledgeStatus.SUPERSEDED)
    with pytest.raises(ValueError):
        replace(ENTRY, superseded_by="DP-0020")


def test_needs_review_after_review_date():
    assert not ENTRY.needs_review(date(2026, 12, 1))
    assert ENTRY.needs_review(date(2026, 12, 2))
    assert not replace(ENTRY, review_by=None).needs_review(date(2030, 1, 1))
