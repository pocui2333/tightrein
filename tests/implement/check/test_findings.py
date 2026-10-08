from __future__ import annotations

from tightrein.implement.check.findings import (
    DESIGN,
    LOCAL,
    NEEDS_USER,
    PLAN_GAP,
    Finding,
    first_category,
    from_facts,
    keys,
    of_first_category,
)


def _finding(kind: str, category: str, location: str | None = "src/a.py:3") -> Finding:
    return Finding("rules", kind, location, f"{kind} 的说明", category)


def test_categories_are_handled_in_order() -> None:
    local, gap, user, design = (_finding("residue", LOCAL), _finding("outside_plan", PLAN_GAP),
                                _finding("forbidden", NEEDS_USER), _finding("design", DESIGN))
    assert first_category([local, gap]) == PLAN_GAP
    assert first_category([local, gap, user]) == NEEDS_USER
    assert first_category([local, gap, user, design]) == DESIGN
    assert first_category([]) is None
    # 交回编码时只给最靠前那一类
    assert of_first_category([local, gap, _finding("temporary_file", LOCAL)]) == [gap]
    assert of_first_category([local]) == [local]


def test_keys_compare_location_and_kind_and_round_trip() -> None:
    first = _finding("residue", LOCAL)
    second = Finding("review", "hardcode", "src/a.py:3", "不同的说明", LOCAL, "输入为 7 时")
    assert keys([second, first]) == [("src/a.py:3", "hardcode"), ("src/a.py:3", "residue")]
    assert _finding("over_cap", LOCAL, None).key() == ("", "over_cap")
    assert from_facts({"blockers": [second.to_json()]}) == [second]
    assert "触发条件：输入为 7 时" in second.text()
