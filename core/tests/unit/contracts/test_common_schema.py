from datetime import date, datetime, timezone

import pytest

from tightrein.contracts import validate
from tightrein.domain import enums, ids
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import KnowledgeType, Probe, RunStage, Stage
from tightrein.domain.issue import Hold
from tightrein.domain.problem import IgnoreCondition, ProblemScope
from tightrein.domain.run import Coverage, Endpoint, EnvironmentDetail, HealthCheck

COMMON = "common.schema.json"
NOW = datetime(2026, 9, 29, 2, 15, 3, tzinfo=timezone.utc)
DEFS = validate.schema(COMMON)["$defs"]
ENUM_DEFS = sorted(name for name in DEFS if name[0].isupper())


def errors(definition, instance):
    return validate.validate(COMMON, instance, definition=definition)


@pytest.mark.parametrize("name", ENUM_DEFS)
def test_enum_definitions_equal_domain_enums(name):
    enum_cls = getattr(enums, name)
    assert enum_cls in enums.ALL_ENUMS
    assert DEFS[name] == {"enum": [member.value for member in enum_cls]}


@pytest.mark.parametrize("definition,value", [
    ("runId", ids.run_id(NOW, RunStage.TRIAGE)),
    ("runId", ids.run_id(NOW, RunStage.COLLECT, Probe.API_FUZZ)),
    ("signalId", ids.signal_id(1_700_000_000_000, bytes(range(10)))),
    ("problemId", ids.problem_id(42)),
    ("issueId", ids.issue_id(7)),
    ("operationId", ids.operation_id(15)),
    ("suggestionId", ids.suggestion_id(21)),
    ("evalId", ids.eval_id(NOW)),
    ("evalCaseId", ids.eval_case_id(4)),
    *[("knowledgeId", ids.knowledge_id(kind, 12)) for kind in KnowledgeType],
    ("timestamp", format_iso(NOW)),
    ("date", date(2026, 10, 5).isoformat()),
])
def test_identifiers_from_domain_match(definition, value):
    assert errors(definition, value) == []


@pytest.mark.parametrize("definition,value", [
    ("runId", "R-20260929-collect"),
    ("problemId", "P-42"),
    ("issueId", "7"),
    ("knowledgeId", "XX-0001"),
    ("timestamp", "2026-09-29T02:15:03+09:00"),
    ("timestamp", "2026-09-29T02:15:03.120Z"),
    ("commit", "D6F37025"),
    ("codeLocation", "src/a.cs"),
    ("fingerprint", "abc"),
])
def test_malformed_identifiers_are_rejected(definition, value):
    assert [error.path for error in errors(definition, value)] == ["$"]


def test_knowledge_id_prefixes_follow_knowledge_types():
    assert DEFS["knowledgeId"]["pattern"] == "^({})-\\d{{4,}}$".format("|".join(k.prefix for k in KnowledgeType))


def test_code_location_accepts_line_range():
    assert errors("codeLocation", "src/Services/Material.cs:42-48") == []


def test_domain_dictionaries_match_definitions():
    coverage = Coverage(endpoints=(Endpoint("GET", "/api/Order/Query", "Company"),), endpoints_total=10,
                        files=("a.cs",), methods="GET", sources=("error-tracking",))
    detail = EnvironmentDetail(health=HealthCheck(200, 31), failed_roles=("Personal",), report_complete=False)
    condition = IgnoreCondition(until=NOW, occurrences=3, new_release=True, baseline_occurrences=5,
                                baseline_release="d6f37025")
    scope = ProblemScope("POST /api/Order/Query", frozenset({"Company"}), "error-tracking")
    hold = Hold("连续 2 次合并前验证失败", Stage.VERIFY, NOW, "见 verify-local-0007")
    assert errors("coverage", coverage.to_dict()) == []
    assert errors("coverage", Coverage().to_dict()) == []
    assert errors("environmentDetail", detail.to_dict()) == []
    assert errors("environmentDetail", EnvironmentDetail().to_dict()) == []
    assert errors("ignoreCondition", condition.to_dict()) == []
    assert errors("ignoreCondition", IgnoreCondition().to_dict()) == []
    assert errors("problemScope", scope.to_dict()) == []
    assert errors("hold", hold.to_dict()) == []


def test_subject_id_follows_type():
    assert errors("subject", {"type": "problem", "id": "P-0042"}) == []
    assert errors("subject", {"type": "week", "id": "2026-10-05"}) == []
    assert [error.path for error in errors("subject", {"type": "problem", "id": "0007"})] == ["$.id"]


def test_flag_requires_reason_and_location_when_flagged():
    assert errors("flag", {"flagged": False}) == []
    assert errors("flag", {"flagged": True, "reason": "要改接口字段", "locations": ["src/a.cs:3"]}) == []
    assert [error.path for error in errors("flag", {"flagged": True, "reason": "要改接口字段", "locations": []})] == [
        "$.locations"]


def test_judged_result_excludes_not_applicable():
    assert errors("judgedResult", "unknown") == []
    assert [error.path for error in errors("judgedResult", "not-applicable")] == ["$"]


def test_coverage_totals_may_be_unknown():
    assert errors("coverage", {"endpointsTotal": None, "sources": ["log-platform"]}) == []
    assert [error.path for error in errors("coverage", {"endpointsTotal": -1})] == ["$.endpointsTotal"]
