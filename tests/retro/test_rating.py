import pytest

from tightrein.retro.rating import RatingRule, rate
from tightrein.retro.records import Impact, Kind, Occurrence, Record, RecordStatus

RULE = RatingRule(repeat=2, frequent=3)


def record(impact: Impact, count: int) -> Record:
    occurrence = Occurrence("R-20261007T093000Z-run", "2026-10-07T09:30:00Z", count, (), ("细节",), impact)
    return Record("0001", "P3", RecordStatus.OPEN, Kind.FAILURE, "implement.code", "implement.code", "step-failed",
                  "事实", "t", "t", occurrences=[occurrence])


@pytest.mark.parametrize("impact, count, rating", [
    (Impact.SEVERE, 2, "P0"),
    (Impact.SEVERE, 1, "P1"),
    (Impact.MAJOR, 1, "P1"),
    (Impact.MINOR, 3, "P1"),
    (Impact.MINOR, 2, "P2"),
    (Impact.TRIVIAL, 9, "P3"),
])
def test_rating_is_impact_times_count(impact: Impact, count: int, rating: str) -> None:
    assert rate(record(impact, count), RULE) == rating


def test_the_worst_occurrence_decides_the_impact() -> None:
    worse = record(Impact.MINOR, 1)
    worse.occurrences.append(Occurrence("R-20261008T093000Z-run", "t", 1, (), ("细节",), Impact.SEVERE))
    assert rate(worse, RULE) == "P0"
