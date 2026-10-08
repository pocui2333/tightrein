from datetime import UTC, datetime

import pytest

from tightrein.collect.alerts.alert_source import alertmanager
from tightrein.collect.common.source import SourceInvalid
from tightrein.protocol import methods

TOKEN = "grafana-token-0123456789abcdef"
ALERTS = "/api/alertmanager/grafana/api/v2/alerts"
FIRING = {"active": "true", "silenced": "false", "inhibited": "false"}


def configured(token=TOKEN):
    return methods.Configured(methods.load("tightrein.collect.alerts.alert_source", "alertmanager"),
                              {"url": "https://grafana.example.test/api/alertmanager/grafana"}, token)


def test_alertmanager_reads_firing_alerts(routes, respond):
    body = [{"fingerprint": "9f2c41", "startsAt": "2026-10-05T02:10:00.123456789+08:00",
             "labels": {"alertname": "OrdersStalled", "severity": "critical"},
             "annotations": {"summary": "订单 1 小时没有进展"}, "generatorURL": "https://grafana/alert/1"},
            {"fingerprint": "77aa", "startsAt": "2026-10-05T02:00:00Z", "labels": {}, "annotations": {}}]
    transport = routes([(ALERTS, FIRING, respond(body))])
    first, second = alertmanager.read(configured(), transport=transport, timeout_s=5)
    assert transport.sent[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert (first.fingerprint, first.name, first.starts_at) == ("9f2c41", "OrdersStalled",
                                                                datetime(2026, 10, 4, 18, 10, tzinfo=UTC))
    assert first.labels["severity"] == "critical" and first.generator_url == "https://grafana/alert/1"
    assert (second.name, second.generator_url) == ("77aa", None)


def test_alertmanager_without_a_token_and_with_a_bad_body(routes, respond):
    transport = routes([(ALERTS, {}, respond([]))])
    assert alertmanager.read(configured(None), transport=transport, timeout_s=5) == []
    assert "Authorization" not in transport.sent[0].headers
    with pytest.raises(SourceInvalid):
        alertmanager.read(configured(None), transport=routes([(ALERTS, {}, respond({"alerts": []}))]), timeout_s=5)
