from datetime import datetime, timezone

import pytest

from tightrein.domain import ids
from tightrein.domain.enums import KnowledgeType, Probe, RunStage

NOW = datetime(2026, 9, 29, 2, 15, 3, tzinfo=timezone.utc)


def test_run_id_for_stage():
    assert ids.run_id(NOW, RunStage.TRIAGE) == "R-20260929-021503-triage"


def test_run_id_for_collect_includes_probe():
    assert ids.run_id(NOW, RunStage.COLLECT, Probe.API_FUZZ) == "R-20260929-021503-collect-api-fuzz"


def test_run_id_for_collect_requires_probe():
    with pytest.raises(ValueError):
        ids.run_id(NOW, RunStage.COLLECT)


def test_run_id_for_non_collect_rejects_probe():
    with pytest.raises(ValueError):
        ids.run_id(NOW, RunStage.TRIAGE, Probe.ALERTS)


def test_sequential_ids():
    assert ids.problem_id(42) == "P-0042"
    assert ids.issue_id(7) == "0007"
    assert ids.operation_id(15) == "OP-0015"
    assert ids.suggestion_id(21) == "LS-0021"
    assert ids.eval_case_id(4) == "E-0004"
    assert ids.knowledge_id(KnowledgeType.DEFECT_PATTERN, 12) == "DP-0012"


def test_sequence_numbers_must_be_positive():
    with pytest.raises(ValueError):
        ids.problem_id(0)


def test_eval_id():
    assert ids.eval_id(NOW) == "EV-20260929-021503"


def test_knowledge_sequence_name():
    assert ids.knowledge_sequence(KnowledgeType.FIX_LESSON) == "knowledge-FL"


def test_signal_id_is_ulid_with_prefix_and_sortable():
    first = ids.signal_id(1_000, bytes(10))
    second = ids.signal_id(2_000, bytes(10))
    assert first.startswith("S-") and len(first) == 28
    assert first < second


def test_signal_id_requires_ten_random_bytes():
    with pytest.raises(ValueError):
        ids.signal_id(1_000, bytes(9))


def test_handoff_ids():
    assert ids.handoff_id("triage", "P-0042") == "triage-P-0042"
    assert ids.verify_handoff_id("local", "0007") == "verify-local-0007"
    assert ids.weekly_handoff_id(datetime(2026, 10, 5, tzinfo=timezone.utc).date()) == "learn-weekly-2026-10-05"


@pytest.mark.parametrize(
    "value,kind",
    [
        ("P-0042", "problem"),
        ("0007", "issue"),
        ("OP-0015", "operation"),
        ("LS-0021", "suggestion"),
        ("E-0004", "eval-case"),
        ("EV-20260929-021503", "eval"),
        ("DP-0012", "knowledge"),
        ("R-20260929-021503-collect-api-fuzz", "run"),
        ("S-" + "0" * 26, "signal"),
    ],
)
def test_kind_of(value, kind):
    assert ids.kind_of(value) == kind


def test_kind_of_unknown_raises():
    with pytest.raises(ValueError):
        ids.kind_of("hello")


def test_parse_sequence():
    assert ids.parse_sequence("P-0042") == 42
    assert ids.parse_sequence("0007") == 7
