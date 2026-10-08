import pytest

from tightrein.assess import refute
from tightrein.assess.refute import RefuteRule, combine, needed


def rule(settings) -> RefuteRule:
    return RefuteRule.from_settings(settings.section("assess"))


def test_refute_is_only_needed_for_high_risk(kit):
    found = rule(kit.make_settings())
    high = {"p0": False, "task_type": "bug", "impact_kind": "non-core-error"}
    assert needed(found, "confirmed", severity="P1", **high)
    assert needed(found, "conditional", severity="P0", **high)
    assert not needed(found, "confirmed", severity="P2", **high)  # P2、P3 取证一次即定
    assert needed(found, "confirmed", severity="P3", p0=False, task_type="security", impact_kind=None)
    assert needed(found, "confirmed", severity="P3", p0=False, task_type="bug", impact_kind="data-correctness")
    assert not needed(found, "insufficient", severity="P0", **high)  # 证据不足不复核
    assert needed(found, "refuted", severity=None, p0=True, task_type=None, impact_kind=None)  # 预估 P0 判为不成立
    assert needed(found, "refuted", severity="P0", p0=False, task_type=None, impact_kind=None)
    assert not needed(found, "refuted", severity="P1", p0=False, task_type=None, impact_kind=None)


def test_the_trigger_comes_from_the_settings(kit):
    found = rule(kit.make_settings({"controls": {"assess": {"refute": {"severities": ["P0"], "taskTypes": [],
                                                                        "impactKinds": []}}}}))
    assert not needed(found, "confirmed", severity="P1", p0=False, task_type="security", impact_kind=None)


@pytest.mark.parametrize("first, second, expected", [
    ("confirmed", "confirmed", ("confirmed", False)),
    ("conditional", "confirmed", ("conditional", False)),
    ("confirmed", "refuted", ("confirmed", True)),  # 保留取证的判定并转人工
    ("confirmed", "insufficient", ("confirmed", True)),
    ("confirmed", None, ("confirmed", True)),  # 复核没通过检查
    ("refuted", "refuted", ("refuted", False)),  # 复核也不成立才算不成立
    ("refuted", "confirmed", ("confirmed", True)),  # 复核判成立就采用复核并转人工
    ("refuted", "conditional", ("conditional", True)),
    ("refuted", "insufficient", ("insufficient", True)),
    ("refuted", None, ("insufficient", True)),
])
def test_refute_combination_table(first, second, expected):
    assert combine(first, second) == expected


def test_an_insufficient_verdict_is_never_refuted():
    with pytest.raises(ValueError):
        combine(refute.INSUFFICIENT, "confirmed")
