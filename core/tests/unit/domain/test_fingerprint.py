import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import Probe, Source
from tightrein.domain.fingerprint import CURRENT_VERSION, canonical, fingerprint
from tightrein.domain.signal import Signal

T = datetime(2026, 9, 29, tzinfo=timezone.utc)


def sig(probe, check, location, message="m", context=None, normalized=None):
    return Signal(
        id="S-" + "0" * 26, run_id="R-20260929-000000-collect-x", source=Source.SYNTHETIC,
        probe=probe, check=check, environment="staging", occurred_at=T, release=None,
        location=location, message=message, context=context or {}, normalized_message=normalized,
    )


def test_fingerprint_is_sha1_prefix_of_canonical_string():
    s = sig(Probe.API_FUZZ, "not_a_server_error", "POST /api/Order/Query", context={"response": {"status": 500}})
    expected = hashlib.sha1(canonical(s, CURRENT_VERSION).encode("utf-8")).hexdigest()[:16]
    assert fingerprint(s, CURRENT_VERSION) == expected
    assert len(expected) == 16


def test_api_fuzz_ignores_role_and_groups_by_status_class():
    a = sig(Probe.API_FUZZ, "not_a_server_error", "POST /api/Order/Query",
            context={"role": "Company", "response": {"status": 500}})
    b = sig(Probe.API_FUZZ, "not_a_server_error", "POST /api/Order/Query",
            context={"role": "Personal", "response": {"status": 503}})
    assert canonical(a, 1) == "v1|api-fuzz|not_a_server_error|POST|/api/Order/Query|5xx"
    assert fingerprint(a, 1) == fingerprint(b, 1)


def test_api_fuzz_authorization_check_includes_role():
    a = sig(Probe.API_FUZZ, "unauthorized_role_access", "GET /api/User",
            context={"role": "Company", "response": {"status": 200}})
    b = replace(a, context={"role": "Personal", "response": {"status": 200}})
    assert canonical(a, 1).endswith("|2xx|Company")
    assert fingerprint(a, 1) != fingerprint(b, 1)


def test_platform_log_with_frames_uses_first_three_without_file_and_line():
    s = sig(Probe.PLATFORM_ERRORS, "error", "Services/A.cs:A.Run",
            context={"exceptionType": "System.NullReferenceException",
                     "projectFrames": ["Services/A.cs:A.Run:10", "B.Call", "Services/C.cs:C.Do:3", "D.More"]})
    assert canonical(s, 1) == "v1|platform-errors|System.NullReferenceException|A.Run|B.Call|C.Do"


def test_platform_log_without_frames_uses_category_and_message():
    s = sig(Probe.PLATFORM_ERRORS, "error", "Microsoft.EntityFrameworkCore",
            context={"category": "Microsoft.EntityFrameworkCore"}, normalized="timeout after <num> ms")
    assert canonical(s, 1) == "v1|platform-errors|Microsoft.EntityFrameworkCore|timeout after <num> ms"


def test_static_and_incidental():
    st = sig(Probe.STATIC, "catch-swallow", "Services/A.cs:A.Run:10")
    inc = sig(Probe.INCIDENTAL, "incidental", "Services/A.cs:A.Run", normalized="吞掉异常")
    assert canonical(st, 1) == "v1|static|catch-swallow|Services/A.cs:A.Run"
    assert canonical(inc, 1) == "v1|incidental|Services/A.cs:A.Run|吞掉异常"


def test_version_changes_fingerprint():
    s = sig(Probe.STATIC, "r", "a.cs:A.B")
    assert fingerprint(s, 1) != fingerprint(s, 2)


def test_regression_signal_has_no_fingerprint():
    s = sig(Probe.API_FUZZ, "regression", "POST /api/Order/Query")
    assert fingerprint(s, 1) is None


def test_message_based_kinds_require_normalized_message():
    s = sig(Probe.INCIDENTAL, "incidental", "a.py:f")
    with pytest.raises(ValueError):
        fingerprint(s, 1)


def test_platform_group_is_the_fingerprint():
    s = sig(Probe.PLATFORM_ERRORS, "error", "app.py:run", context={"platformGroup": "sentry:acme/4512"})
    assert fingerprint(s, 1) == "sentry:acme/4512"
    alert = sig(Probe.ALERTS, "business-alert", "OrdersStalled", context={"platformGroup": "alertmanager:9f2c"})
    assert fingerprint(alert, 2) == "alertmanager:9f2c"


def test_project_probe_fingerprint_uses_probe_name_and_given_value():
    a = sig(Probe.PROJECT_PROBE, "daily-import", "job:import",
            context={"sourceName": "daily-import", "probeFingerprint": "missed:import"})
    b = replace(a, location="job:other")
    other = replace(a, context={"sourceName": "nightly", "probeFingerprint": "missed:import"})
    assert canonical(a, 1) == "v1|project-probe|daily-import|missed:import"
    assert fingerprint(a, 1) == fingerprint(b, 1) != fingerprint(other, 1)
