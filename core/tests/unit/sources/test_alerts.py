from platform_world import ALERT_SOURCE, PlatformClient, broken, ok
from probe_world import RELEASE, CountingRandom, make_redactor, make_target

from tightrein.domain.enums import RunStatus, Source
from tightrein.sources.alerts.source import DISABLED, AlertsDependencies, AlertsSource, excluded
from tightrein.sources.base import ProbeOptions


def alert(name, labels=None, **annotations):
    return {"fingerprint": f"fp-{name}", "name": name, "labels": {"alertname": name, **(labels or {})},
            "annotations": annotations, "startsAt": "2026-10-05T02:00:00Z", "generatorUrl": None}


def run(tmp_path, make_config, client):
    found = AlertsSource(AlertsDependencies(make_config(), client, make_redactor(), lambda at: RELEASE,
                                            CountingRandom()))
    return found.run(make_target(tmp_path, "alerts"), None, ProbeOptions())


def test_infrastructure_alerts_are_excluded_by_name_or_label(make_config):
    exclude = make_config().get("sources.alerts.exclude")
    names, labels = exclude["names"], exclude["labels"]
    assert excluded(alert("NodeDiskFull"), names, labels)
    assert excluded(alert("PodRestarting"), names, labels)
    assert excluded(alert("QueueSlow", {"category": "infrastructure"}), names, labels)
    assert not excluded(alert("OrdersStalled", {"severity": "critical"}), names, labels)


def test_business_alerts_become_signals_with_the_alert_fingerprint(tmp_path, make_config):
    client = PlatformClient({ALERT_SOURCE}, alert_source=ok(ALERT_SOURCE, {"alerts": [
        alert("OrdersStalled", {"severity": "critical"}, summary="订单 1 小时没有进展"), alert("NodeMemoryHigh")]}))
    outcome = run(tmp_path, make_config, client)
    assert outcome.status is RunStatus.OK and outcome.stats["excludedAlerts"] == 1
    [signal] = outcome.signals
    assert (signal.source, signal.check, signal.location, signal.message) == (
        Source.BEHAVIOR, "business-alert", "OrdersStalled", "订单 1 小时没有进展")
    assert signal.context["platformGroup"] == "alertmanager:fp-OrdersStalled"
    assert signal.context["severityHint"] == "critical" and outcome.coverage.sources == ("alert-source",)


def test_disabled_or_failed(tmp_path, make_config):
    assert run(tmp_path, make_config, PlatformClient()).skipped_reason == DISABLED
    outcome = run(tmp_path, make_config, PlatformClient({ALERT_SOURCE}, alert_source=broken(ALERT_SOURCE)))
    assert outcome.status is RunStatus.FAILED
