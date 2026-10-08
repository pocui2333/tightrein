from datetime import UTC, datetime

from tightrein.assess.tradeoff import locations_of, tradeoff_valid
from tightrein.knowledge.entries import Entry, EntryStatus
from tightrein.knowledge.match import classify
from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)


def entry(entry_id: str, kind: str = "conventions", status: EntryStatus = EntryStatus.ACTIVE) -> Entry:
    return Entry(id=entry_id, kind=kind, title="t", summary="s", body="b", status=status)


def test_tradeoffs_are_checked_against_the_given_knowledge():
    given = [entry("CON-0001"), entry("PAT-0002", kind="patterns"), entry("CON-0003", status=EntryStatus.SUPERSEDED)]
    assert tradeoff_valid("CON-0001", given)
    assert not tradeoff_valid("CON-0009", given)  # 编号不在给出的条目中：模型编的
    assert not tradeoff_valid("PAT-0002", given)  # 不是取舍
    assert not tradeoff_valid("CON-0003", given)  # 已取代
    assert not tradeoff_valid(None, given)


def test_without_given_paths_the_locations_come_from_the_problem_and_its_signals():
    problem = Problem(id="P-0001", fingerprint="fp", source="collect.api_fuzz", check_type="server_error",
                      status="new", title="t", first_seen=NOW, last_seen=NOW, location="GET /api/orders")
    found = [Occurrence("P-0001", NOW, "collect.api_fuzz", evidence={"location": "GET /api/orders"}),
             Occurrence("P-0001", NOW, "collect.platform_errors", evidence={"location": "/orders"}),
             Occurrence("P-0001", NOW, "collect.static", evidence={"location": "src/orders.py:12"}),
             Occurrence("P-0001", NOW, "collect.alerts", evidence={})]
    values = locations_of(problem, found, [])
    assert values == ["GET /api/orders", "/orders", "src/orders.py:12"]
    kinds = classify(values)  # 先分类再匹配
    assert (kinds.routes, kinds.pages, kinds.paths) == (("GET /api/orders",), ("/orders",), ("src/orders.py",))
    assert locations_of(problem, [], ["src/a.py"]) == ["src/a.py", "GET /api/orders"]
