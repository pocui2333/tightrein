from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import Probe, RunStage, RunStatus
from tightrein.domain.run import Coverage, Endpoint, EnvironmentDetail, HealthCheck, Run

T = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)


def test_coverage_round_trip_uses_schema_keys():
    data = {
        "endpoints": [{"method": "POST", "route": "/api/Order/Query", "role": "Company"}],
        "endpointsTotal": 120,
        "methods": "all",
    }
    coverage = Coverage.from_dict(data)
    assert coverage.endpoints == (Endpoint("POST", "/api/Order/Query", "Company"),)
    assert coverage.endpoints_total == 120
    assert coverage.to_dict() == data


def test_source_coverage():
    assert Coverage.from_dict({"sources": ["error-tracking"]}).sources == ("error-tracking",)
    assert Coverage.from_dict({}).sources == ()
    assert Coverage(sources=("alert-source",)).to_dict() == {"sources": ["alert-source"]}


def test_methods_must_be_get_or_all():
    with pytest.raises(ValueError):
        Coverage(methods="POST")


def test_covers_endpoint_with_role():
    coverage = Coverage(endpoints=(Endpoint("GET", "/api/Material/{id}", "Company"),), methods="GET")
    assert coverage.covers_endpoint("GET", "/api/Material/{id}", "Company")
    assert not coverage.covers_endpoint("GET", "/api/Material/{id}", "Personal")
    assert coverage.covers_endpoint("GET", "/api/Material/{id}", None)


def test_shallow_run_does_not_cover_write_endpoints():
    coverage = Coverage(endpoints=(Endpoint("POST", "/api/Order/Query", "Company"),), methods="GET")
    assert not coverage.covers_endpoint("POST", "/api/Order/Query", "Company")


def test_tested_endpoints_ignore_role():
    coverage = Coverage(endpoints=(Endpoint("GET", "/a", "Company"), Endpoint("GET", "/a", "Personal")))
    assert coverage.tested_endpoints() == frozenset({("GET", "/a")})


def test_environment_detail_round_trip():
    data = {"health": {"status": 200, "elapsedMs": 35}, "failedRoles": ["Personal"], "reportComplete": True}
    detail = EnvironmentDetail.from_dict(data)
    assert detail.health == HealthCheck(200, 35)
    assert detail.health.ok
    assert detail.failed_roles == ("Personal",)
    assert detail.to_dict() == data


def test_health_check_failure():
    assert not HealthCheck(None).ok
    assert not HealthCheck(503).ok
    assert EnvironmentDetail.from_dict({"health": None}).health is None


def test_collect_run_requires_probe():
    with pytest.raises(ValueError):
        Run(id="R-20260929-021500-collect-e2e", stage=RunStage.COLLECT, started_at=T, status=RunStatus.OK)
    with pytest.raises(ValueError):
        Run(id="R-20260929-021500-triage", stage=RunStage.TRIAGE, started_at=T, status=RunStatus.OK,
            probe=Probe.STATIC)


def test_run_requires_timezone():
    with pytest.raises(ValueError):
        Run(id="R-20260929-021500-triage", stage=RunStage.TRIAGE, started_at=datetime(2026, 9, 29),
            status=RunStatus.OK)
