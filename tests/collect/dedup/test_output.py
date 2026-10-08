from tightrein.collect.dedup.output import SAMPLE_LIMIT, samples


def record(role, location, index):
    return {"location": location, "evidence": {"role": role}, "occurredAt": f"2026-10-07T10:{index:02d}:00Z"}


def test_samples_are_distinct_by_role_and_location_newest_first():
    latest = record("Company", "GET /api/a", 59)
    found = [record("Company", "GET /api/a", 58), record("Personal", "GET /api/a", 57),
             record("Personal", "GET /api/a", 56), record("Company", "GET /api/b", 55)]
    picked = samples(found, latest)
    assert [(item["evidence"]["role"], item["location"]) for item in picked] == [
        ("Personal", "GET /api/a"), ("Company", "GET /api/b")]
    assert picked[0]["occurredAt"].endswith("10:57:00Z")


def test_repeats_do_not_take_the_places_of_other_samples():
    latest = record(None, "a.py", 59)
    found = [record(None, "a.py", 50)] * 20 + [record(None, f"f{index}.py", index) for index in range(10)]
    picked = samples(found, latest)
    assert len(picked) == SAMPLE_LIMIT == 5
    assert [item["location"] for item in picked] == ["f0.py", "f1.py", "f2.py", "f3.py", "f4.py"]
