import json
from pathlib import Path

import pytest

from tightrein.assess import rating

SIZES = {"small": {"files": 3}, "medium": {"files": 10}}
SCHEMA = Path(rating.__file__).with_name("assess.triage.schema.json")


def test_every_impact_kind_in_the_output_schema_is_mapped():
    kinds = json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]["impact"]["properties"]["kind"]["enum"]
    assert set(rating.SEVERITY_BY_IMPACT) == set(kinds)


def test_the_assessed_severity_wins_over_the_impact_mapping(good_output):
    output = good_output()  # impact non-core-error 映射为 P2，报告给的是 P2
    output["report"]["severity"] = "P1"
    assert rating.rate(output, p0=False, size_limits=SIZES).severity == "P1"
    output["report"]["severity"] = None
    assert rating.rate(output, p0=False, size_limits=SIZES).severity == "P2"
    output["impact"] = None
    assert rating.rate(output, p0=True, size_limits=SIZES).severity == "P0"  # 没有影响面但预估 P0 的仍记 P0
    assert rating.rate(output, p0=False, size_limits=SIZES).severity is None


@pytest.mark.parametrize("given, files, expected", [
    ("small", 1, "small"), ("small", 4, "medium"), ("medium", 2, "medium"), ("small", 11, "large"),
    ("large", 1, "large"),
])
def test_size_is_the_larger_of_the_model_and_the_file_count(good_output, given, files, expected):
    output = good_output()
    output["assessment"]["size"] = given
    output["assessment"]["files"] = [{"path": f"src/f{number}.py", "isNew": True} for number in range(files)]
    found = rating.rate(output, p0=False, size_limits=SIZES)
    assert found.size == expected and len(found.files) == files and found.task_type == "bug"


def test_without_assessment_files_come_from_root_causes(outputs):
    found = rating.rate(outputs.refuted(rootCauses=[{"file": "a.py", "line": 1, "symbol": None},
                                                    {"file": "a.py", "line": 2, "symbol": None}]),
                        p0=False, size_limits=SIZES)
    assert found.files == ("a.py",) and found.size is None
    assert rating.rate(None, p0=True, size_limits=SIZES).severity == "P0"


def test_higher_picks_the_more_severe():
    assert rating.higher("P2", "P1") == "P1"
    assert rating.higher(None, "P3") == "P3"
    assert rating.higher(None, None) is None
