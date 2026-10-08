import re

import pytest

from tightrein.collect.alerts import source
from tightrein.collect.alerts.alert_source.alertmanager import Alert
from tightrein.collect.common.source import SourceMisconfigured, SourceStatus, SourceUnavailable
from tightrein.protocol.http import HttpResponse

ALERTS = "/api/v2/alerts"
SITES = {"alertmanager": {"url": "https://alertmanager.example.test"}}


def alert(name, labels=None, **annotations):
    return {"fingerprint": f"fp-{name}", "startsAt": "2026-10-05T02:00:00Z",
            "labels": {"alertname": name, **(labels or {})}, "annotations": annotations}


def runtime(source_runtime, method="alertmanager"):
    return source_runtime(modules={source.SOURCE: {"method": method}}, sites=SITES)


def test_infrastructure_alerts_are_excluded_by_name_or_label(source_runtime):
    exclude = runtime(source_runtime).settings.section(source.SOURCE)["exclude"]
    names, labels = [re.compile(item) for item in exclude["names"]], exclude["labels"]

    def made(name, extra=None):
        return Alert(f"fp-{name}", name, {"alertname": name, **(extra or {})}, {}, None, None)

    assert source.excluded(made("NodeDiskFull"), names, labels)
    assert source.excluded(made("PodRestarting"), names, labels)
    assert source.excluded(made("QueueSlow", {"category": "infrastructure"}), names, labels)
    assert not source.excluded(made("OrdersStalled", {"severity": "critical"}), names, labels)


def test_business_alerts_become_signals_with_the_alert_fingerprint(source_runtime, routes, respond):
    body = [alert("OrdersStalled", {"severity": "critical"}, summary="订单 1 小时没有进展"), alert("NodeMemoryHigh"),
            alert("PaymentsFailing", description="支付失败率升高"), alert("Quiet")]
    result = source.collect(runtime(source_runtime), routes([(ALERTS, {}, respond(body))]))
    assert result.status is SourceStatus.DONE and result.metrics.produced["excludedAlerts"] == 1
    assert result.coverage == ["alertmanager"] and result.state == {}
    first, second, third = result.signals
    assert (first.check_type, first.location, first.message, first.group_key) == (
        "alert", "OrdersStalled", "订单 1 小时没有进展", "alertmanager:fp-OrdersStalled")
    assert first.evidence["severity"] == "critical" and first.occurred_at == "2026-10-05T02:00:00Z"
    assert (second.message, third.message) == ("支付失败率升高", "Quiet")


def test_disabled_misconfigured_or_unavailable(source_runtime, routes):
    assert source.collect(source_runtime(), routes([])).status is SourceStatus.SKIPPED
    with pytest.raises(SourceMisconfigured):
        source.collect(runtime(source_runtime, method="pagerduty"), routes([]))
    with pytest.raises(SourceUnavailable):
        source.collect(runtime(source_runtime), routes([(ALERTS, {}, HttpResponse(503, b"[]"))]))
