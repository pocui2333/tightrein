import pytest
from contract_samples import changed, envelope, paths, problem, signal, without


ENVELOPE = "handoff/envelope.schema.json"
SIGNAL = "data/signal.schema.json"
PROBLEM = "data/problem.schema.json"

REGRESSION_CONTEXT = {"issue": "0007", "checkId": "api-1", "targetFingerprints": ["0123456789abcdef"],
                      "detail": "状态码 500"}
IGNORE_UNTIL = {"until": None, "occurrences": 3, "newRelease": False, "severityEscalated": True,
                "baselineOccurrences": 4, "baselineRelease": "d6f37025"}

VALID = [
    (ENVELOPE, envelope()),
    (ENVELOPE, envelope(status="blocked", blockedReason="staging 不可用")),
    (ENVELOPE, envelope(stage="loop", subject={"type": "run", "id": "R-20260929-021503-loop"})),
    (ENVELOPE, envelope(stage="learn", subject={"type": "week", "id": "2026-10-05"})),
    (SIGNAL, signal()),
    (SIGNAL, changed(signal(), probe="platform-errors", source="error", check="error", actor={}, release=None)),
    (SIGNAL, changed(signal(), probe="platform-errors", source="error", check="frontend-error", actor={},
                     fingerprint="sentry:acme/4512")),
    (SIGNAL, changed(signal(), probe="alerts", source="behavior", check="business-alert", actor={},
                     fingerprint="alertmanager:9f2c0a")),
    (SIGNAL, changed(signal(), check="regression", context=REGRESSION_CONTEXT)),
    (PROBLEM, problem()),
    (PROBLEM, changed(problem(), status="ignored", ignoreUntil=IGNORE_UNTIL, issueId="0007")),
]

INVALID = [
    (ENVELOPE, without(envelope(), "schemaVersion"), "$"),
    (ENVELOPE, envelope(stage="deploy"), "$.stage"),
    (ENVELOPE, envelope(status="failed"), "$.blockedReason"),
    (ENVELOPE, envelope(subject={"type": "issue", "id": "P-0042"}), "$.subject.id"),
    (ENVELOPE, envelope(createdAt="2026-09-29 02:30:00"), "$.createdAt"),
    (SIGNAL, changed(signal(), source="log"), "$.source"),
    (SIGNAL, changed(signal(), message="长" * 1001), "$.message"),
    (SIGNAL, changed(signal(), check="regression"), "$.context"),
    (SIGNAL, changed(signal(), extra=1), "$"),
    (SIGNAL, changed(signal(), probe="server-log"), "$.probe"),
    (SIGNAL, changed(signal(), fingerprint="sentry acme"), "$.fingerprint"),
    (PROBLEM, changed(problem(), status="flaky"), "$.status"),
    (PROBLEM, changed(problem(), title="题" * 121), "$.title"),
    (PROBLEM, changed(problem(), occurrences=0), "$.occurrences"),
    (PROBLEM, changed(problem(), ignoreUntil={"until": None}), "$.ignoreUntil"),
    (PROBLEM, without(problem(), "scope"), "$"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)
