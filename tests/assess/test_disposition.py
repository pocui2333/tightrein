import pytest

from tightrein.assess.disposition import (
    DISCUSS,
    Decision,
    Destination,
    Facts,
    TreatmentTreeInvalid,
    decide,
    treatment,
    tree,
)


@pytest.fixture
def rules(kit):
    return tree(kit.make_settings().section("assess")["treatment"])


@pytest.mark.parametrize("verdict, severity, size, worth, expected", [
    ("confirmed", "P0", "large", "wont", "wont_fix"),
    ("conditional", "P0", "large", "defer", "fix_now"),
    ("confirmed", "P1", "medium", "fix", "fix_now"),
    ("conditional", "P1", "medium", "fix", "fix_later"),
    ("confirmed", "P2", "small", "defer", "watch"),
    ("confirmed", "P2", "small", "fix", "fix_later"),
    ("confirmed", "P3", "small", "fix", "fix_later"),
    ("confirmed", "P3", "medium", "fix", "watch"),
    ("confirmed", None, None, None, "watch"),
])
def test_default_treatment_tree(rules, verdict, severity, size, worth, expected):
    assert treatment(rules, verdict, severity, size, worth) == expected


def test_tree_without_fallback_is_an_error(kit):
    items = kit.make_settings().section("assess")["treatment"]
    with pytest.raises(TreatmentTreeInvalid):
        tree(items[:2])
    with pytest.raises(TreatmentTreeInvalid):
        tree([{"treatment": "immediate"}])
    with pytest.raises(TreatmentTreeInvalid):
        treatment(tree(items)[:2], "confirmed", "P3", None, None)


@pytest.mark.parametrize("facts, expected", [
    (Facts("insufficient", "P1"), Destination.WATCH_EVIDENCE),  # 证据不足先转观察
    (Facts("insufficient", "P1", insufficient_count=3), Destination.MANUAL),  # 连续几次仍不足才转人工
    (Facts("confirmed", "P1", needs_manual=True), Destination.MANUAL),
    (Facts("refuted", "P2"), Destination.FALSE_POSITIVE),
    (Facts("refuted", "P0", refuter_verdict="refuted"), Destination.FALSE_POSITIVE),
    (Facts("confirmed", "P1", fixed_on_main=True), Destination.AWAITING_DEPLOY),
    (Facts("confirmed", "P2", worth="fix", tradeoff_hit=True), Destination.TRADEOFF),
    (Facts("confirmed", "P2", "small", "fix"), Destination.ISSUE),
    (Facts("conditional", "P2", "small", "fix"), Destination.ISSUE),
    (Facts("confirmed", "P3", "medium", "fix"), Destination.WATCH),
    (Facts("confirmed", "P3", "small", "wont"), Destination.WONT_FIX),
])
def test_disposition_rows(rules, facts, expected):
    assert decide(facts, rules).destination is expected


@pytest.mark.parametrize("facts", [
    Facts("confirmed", "P0", worth="wont"),
    Facts("conditional", "P0", tradeoff_hit=True),  # P0 排在取舍之前，不会被取舍吞掉
    Facts("confirmed", "P0"),
])
def test_p0_confirmed_always_creates_issue(rules, facts):
    assert decide(facts, rules) == Decision(Destination.ISSUE, "fix_now")


def test_p0_fixed_on_main_waits_for_deploy(rules):
    assert decide(Facts("confirmed", "P0", fixed_on_main=True), rules).destination is Destination.AWAITING_DEPLOY


def test_p0_refuted_requires_the_refuter(rules):
    with pytest.raises(ValueError):
        decide(Facts("refuted", "P0"), rules)
    with pytest.raises(ValueError):
        decide(Facts("refuted", "P0", refuter_verdict="insufficient"), rules)


def test_a_second_watch_becomes_an_issue(rules):
    first = decide(Facts("confirmed", "P3", "medium", "fix"), rules)
    again = decide(Facts("confirmed", "P3", "medium", "fix", observed_before=True), rules)
    assert first.destination is Destination.WATCH
    assert again == Decision(Destination.ISSUE, "fix_later")


def test_labels_discuss_when_protected(rules):
    found = decide(Facts("confirmed", "P2", "small", "fix", touches_protected=True), rules)
    assert found.labels == (DISCUSS,) and found.disposition == "fix_later"
    assert decide(Facts("confirmed", "P2", "small", "fix"), rules).labels == ()


def test_every_destination_maps_to_one_of_the_four_dispositions(rules):
    for facts in (Facts("insufficient"), Facts("refuted", "P2"), Facts("confirmed", "P1", fixed_on_main=True),
                  Facts("confirmed", "P3", "small", "wont")):
        assert decide(facts, rules).disposition in ("fix_now", "fix_later", "watch", "wont_fix")
