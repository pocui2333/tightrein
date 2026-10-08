from pathlib import Path

import pytest

from tightrein.retro import records
from tightrein.retro.records import Impact, Kind, Occurrence, Record, RecordNotFound, RecordStatus
from tightrein.store.files.layout import WorkspaceLayout

RUN = "R-20261007T093000Z-run"


def record(number: str = "0003", rating: str = "P1", status: RecordStatus = RecordStatus.OPEN) -> Record:
    occurrence = Occurrence(RUN, "2026-10-07T09:30:00Z", 2, ("0018",),
                            ("Issue 0018 的 implement.review 来回 5 轮", "Issue 0018 的 implement.review 来回 4 轮"),
                            Impact.MINOR, tokens=1200, duration_ms=60_000, rounds=5)
    return Record(number, rating, status, Kind.WASTE, "implement.review", "implement.review", "many-rounds",
                  "同一步来回多轮", "2026-10-07T09:30:00Z", "2026-10-07T09:30:00Z", idea="把阈值调低", settle=None,
                  occurrences=[occurrence])


def test_the_file_name_shows_number_rating_location_and_short_name(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    saved = records.save(layout, record())
    assert saved.path == layout.retro_dir / "0003-P1-implement.review-many-rounds.md"
    text = saved.path.read_text(encoding="utf-8")
    assert "# 0003 浪费：同一步来回多轮" in text and "### R-20261007T093000Z-run" in text
    assert "| 解决思路 | 把阈值调低 |" in text and "返工 5 轮" in text


def test_records_round_trip_and_a_new_rating_renames_the_file(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    saved = records.save(layout, record())
    loaded = records.get(layout, "3")
    assert loaded.to_json() == saved.to_json() and loaded.count == 2 and loaded.subjects == {"0018"}
    loaded.rating = "P0"
    records.save(layout, loaded)
    assert [path.name for path in layout.retro_dir.iterdir()] == ["0003-P0-implement.review-many-rounds.md"]
    with pytest.raises(RecordNotFound):
        records.get(layout, "4")


def test_close_and_list_by_rating(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    for number, rating in (("0001", "P2"), ("0002", "P0"), ("0003", "P1")):
        records.save(layout, record(number, rating))
    records.set_status(layout, "0003", RecordStatus.WONTFIX)
    assert [item.id for item in records.listing(layout)] == ["0002", "0001"]
    assert records.get(layout, "0003").status is RecordStatus.WONTFIX
    assert records.next_id(records.load_all(layout)) == "0004"
    assert records.next_id([]) == "0001"


def test_call_points_are_grouped_before_fingerprinting() -> None:
    assert records.call_point_group("collect.static.verify-2") == "collect.static.verify"
    assert records.call_point_group("collect.static.review-sq-3") == "collect.static.review"
    assert records.call_point_group("implement.review.deep") == "implement.review.deep"
    assert records.fingerprint(Kind.FAILURE, "collect.static", "collect.static.verify-2", "call-timeout") == \
        records.fingerprint(Kind.FAILURE, "collect.static", "collect.static.verify-7", "call-timeout")
