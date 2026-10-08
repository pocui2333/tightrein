import hashlib
from datetime import UTC, datetime

from tightrein.collect.dedup import group
from tightrein.collect.dedup.changes import ChangeSet
from tightrein.collect.dedup.group import Params, apply, apply_occurrence, canonical, fingerprint, title_for
from tightrein.collect.dedup.status import CLEAN_RUNS, ProblemStatus

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RUN = "R-20261007T120000Z-collect"
PARAMS = Params(title_length=120, nearby_lines=10)


def changeset() -> ChangeSet:
    return ChangeSet(run=RUN, now=NOW, next_number=1)


def test_fingerprint_is_sha1_prefix_of_versioned_canonical_string(make_signal):
    signal = make_signal(source="collect.api_fuzz", check_type="server_error", location="POST /api/Order/Query",
                         evidence={"status": 500})
    value = canonical(signal, "m")
    assert value == "v2|collect.api_fuzz|server_error|POST|/api/Order/Query|5xx"
    assert fingerprint(signal, "m") == hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    assert fingerprint(signal, "m", version=1) != fingerprint(signal, "m")


def test_api_fuzz_ignores_role_and_groups_by_status_class(make_signal):
    a = make_signal(source="collect.api_fuzz", check_type="server_error", location="POST /api/Order/12",
                    evidence={"role": "Company", "status": 502})
    b = make_signal(source="collect.api_fuzz", check_type="server_error", location="POST /api/Order/99",
                    evidence={"role": "Personal", "status": 503})
    assert fingerprint(a, "x") == fingerprint(b, "y")


def test_platform_group_is_the_fingerprint(make_signal):
    signal = make_signal(group_key="sentry:acme/4512")
    assert fingerprint(signal, "m") == "sentry:acme/4512"


def test_platform_log_with_frames_uses_first_three_symbols_without_file_and_line(make_signal):
    frames = ["Services/A.cs:A.Run:10", "B.Call", "Services/C.cs:C.Do:3", "D.More"]
    signal = make_signal(evidence={"exceptionType": "NullReference", "projectFrames": frames})
    assert canonical(signal, "m") == "v2|collect.platform_errors|error|NullReference|A.Run|B.Call|C.Do"


def test_platform_log_without_frames_uses_category_and_message(make_signal):
    signal = make_signal(location=None, evidence={"category": "Microsoft.EntityFrameworkCore"})
    assert canonical(signal, "timeout after <num> ms") == \
        "v2|collect.platform_errors|error|Microsoft.EntityFrameworkCore|timeout after <num> ms"


def test_project_probe_uses_probe_name_and_given_value(make_signal):
    a = make_signal(source="collect.project_probes", check_type="probe", location="job:import",
                    evidence={"sourceName": "daily-import", "probeFingerprint": "missed:import"})
    b = make_signal(source="collect.project_probes", check_type="probe", location="job:other",
                    evidence={"sourceName": "daily-import", "probeFingerprint": "missed:import"})
    other = make_signal(source="collect.project_probes", check_type="probe", location="job:import",
                        evidence={"sourceName": "nightly", "probeFingerprint": "missed:import"})
    assert canonical(a, "m") == "v2|collect.project_probes|probe|daily-import|missed:import"
    assert fingerprint(a, "m") == fingerprint(b, "m") != fingerprint(other, "m")


def test_static_ignores_line_numbers_and_check_type_separates_problems(make_signal):
    a = make_signal(source="collect.static", check_type="static", location="a.py:10", symbol="run",
                    evidence={"rule": "swallow"})
    moved = make_signal(source="collect.static", check_type="static", location="a.py:30", symbol="run",
                        evidence={"rule": "swallow"})
    assert canonical(a, "m") == "v2|collect.static|static|swallow|a.py:run"
    assert fingerprint(a, "m") == fingerprint(moved, "m")
    slow = make_signal(check_type="latency", location="a.py:10", symbol="run")
    error = make_signal(check_type="error", location="a.py:10", symbol="run")
    assert fingerprint(slow, "m") != fingerprint(error, "m")


def test_titles_use_stable_fields_and_are_truncated(make_signal):
    api = make_signal(source="collect.api_fuzz", check_type="server_error", location="GET /api/x/12",
                      evidence={"status": 500})
    assert title_for(api, "m", 120) == "GET /api/x/{id} server_error 500"
    static = make_signal(source="collect.static", check_type="static", location="a.py:3", symbol="f",
                         evidence={"rule": "swallow"})
    assert title_for(static, "m", 120) == "swallow：a.py:f"
    platform = make_signal(evidence={"exceptionType": "KeyError"})
    assert title_for(platform, "KeyError: 'x'", 120) == "KeyError: 'x'"
    log = make_signal(location=None, evidence={"category": "db"})
    assert title_for(log, "timeout", 120) == "db：timeout"
    assert len(title_for(log, "x" * 500, 50)) == 50


def test_grouping_creates_once_and_appends_by_fingerprint(make_signal):
    found = changeset()
    first, second = make_signal(group_key="g1"), make_signal(group_key="g1", occurred_at="2026-10-07T11:30:00Z")
    apply(found, [first, second], {first.id: "boom", second.id: "boom"}, PARAMS)
    assert found.created == ["P-0001"]
    problem = found.known["P-0001"]
    assert problem.status == ProblemStatus.PENDING and problem.count == 2
    assert [item.problem for item in found.occurrences] == ["P-0001", "P-0001"]


def test_occurrences_out_of_order_keep_release_and_reset_clean_runs(make_signal):
    found = changeset()
    signal = make_signal(group_key="g", occurred_at="2026-10-07T10:00:00Z", commit="c2")
    apply(found, [signal], {signal.id: "m"}, PARAMS)
    problem = found.known["P-0001"]
    problem.extra[CLEAN_RUNS] = 2
    apply_occurrence(problem, make_signal(group_key="g", occurred_at="2026-10-07T09:00:00Z", commit="c1"))
    assert problem.first_seen.hour == 9 and problem.last_seen.hour == 10 and problem.last_commit == "c2"
    apply_occurrence(problem, make_signal(group_key="g", occurred_at="2026-10-07T11:00:00Z", commit=None))
    assert problem.last_seen.hour == 11 and problem.last_commit == "c2"
    assert problem.extra[CLEAN_RUNS] == 0 and problem.count == 3


def test_same_function_from_another_source_is_merged_with_both_fingerprints(make_signal):
    found = changeset()
    static = make_signal(source="collect.static", check_type="static", location="svc.py:40", symbol="query",
                         evidence={"rule": "null-deref"})
    runtime_error = make_signal(source="collect.platform_errors", check_type="error", location="svc.py:44",
                                symbol="query", message="NoneType")
    far = make_signal(source="collect.incidental", check_type="incidental:bug", location="svc.py:400", symbol=None)
    messages = {item.id: item.message for item in (static, runtime_error, far)}
    apply(found, [static, runtime_error, far], messages, PARAMS)
    assert found.created == ["P-0001", "P-0002"]
    merged = found.known["P-0001"]
    assert merged.count == 2 and found.merged == 1
    assert merged.extra[group.SOURCES] == ["collect.static", "collect.platform_errors"]
    assert fingerprint(runtime_error, "NoneType") in merged.extra["aliases"]
    assert found.by_fingerprint(fingerprint(runtime_error, "NoneType")) is merged


def test_performance_and_errors_at_the_same_place_are_not_merged(make_signal):
    found = changeset()
    error = make_signal(source="collect.static", check_type="static", location="svc.py:40", symbol="query")
    slow = make_signal(source="collect.access_log", check_type="latency", location="svc.py:40", symbol="query")
    apply(found, [error, slow], {error.id: "m", slow.id: "m"}, PARAMS)
    assert found.created == ["P-0001", "P-0002"]


def test_regression_signals_go_to_their_target_or_are_noted(make_signal):
    found = changeset()
    original = make_signal(group_key="g")
    apply(found, [original], {original.id: "m"}, PARAMS)
    back = make_signal(source="collect.project_probes", check_type="probe", evidence={"targetFingerprints": ["g"]})
    lost = make_signal(source="collect.project_probes", check_type="probe", evidence={"targetFingerprints": ["zz"]})
    apply(found, [back, lost], {back.id: "m", lost.id: "m"}, PARAMS)
    assert found.created == ["P-0001"] and found.known["P-0001"].count == 2
    assert any("zz" in note for note in found.notes)
    assert found.regression_checks == {"P-0001"}  # 记下该问题本次是复现检查失败
