from tightrein.domain.commit_facts import CommitFacts

FACTS = CommitFacts.of([("a1", "b2", True), ("b2", "a1", False), ("c3", "b2", False)])


def test_known_ancestry():
    assert FACTS.is_ancestor("a1", "b2") is True
    assert FACTS.is_ancestor("b2", "a1") is False


def test_same_commit_is_its_own_ancestor():
    assert FACTS.is_ancestor("a1", "a1") is True


def test_unknown_pair_is_none():
    assert FACTS.is_ancestor("a1", "zz") is None


def test_is_newer_requires_ancestor_and_distinct():
    assert FACTS.is_newer("b2", than="a1") is True
    assert FACTS.is_newer("a1", than="b2") is False
    assert FACTS.is_newer("b2", than="b2") is False


def test_sibling_branch_is_not_newer():
    # c3 不是 b2 的祖先：b2 不算晚于 c3
    assert FACTS.is_newer("b2", than="c3") is False


def test_is_newer_unknown_when_missing():
    assert FACTS.is_newer("b2", than=None) is None
    assert FACTS.is_newer(None, than="a1") is None
    assert FACTS.is_newer("zz", than="a1") is None
