"""AlertsSource(redesign/01-collect.md 第 4 节)：经 extensions.alert-source 读取已触发、未静默的告警。

基础设施类告警(磁盘、内存、重启等)按 sources.alerts.exclude 排除：告警名匹配 names 中任一正则，或某个标签的取值
在 labels 给出的清单中。其余每条告警一条信号：source 为 behavior，check 为 business-alert，location 为告警名，
message 取注解 summary(没有时为 description，再没有时为告警名)，occurred_at 为告警开始时间，严重度提示取标签
severity；指纹为平台的告警分组编号 alertmanager:<fingerprint>。没有配置 alert-source 时为 skipped(未启用)。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import ExtensionPoint, ProbeLevel, RunStatus, Source
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.fingerprint import PLATFORM_GROUP
from tightrein.domain.run import Coverage
from tightrein.extensions.client import ExtensionClient
from tightrein.sources.base import PROBE_LEVELS, ProbeOptions, ProbeOutcome, ProbeTarget, failed, skipped
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import RandomBytes, SignalFactory
from tightrein.sources.platform_errors.logs import ReleaseAt
from tightrein.sources.platform_errors.source import failure_text

SOURCE_NAME = "alert-source"
CHECK = "business-alert"
GROUP_PREFIX = "alertmanager:"
MESSAGE_KEYS = ("summary", "description", "message")
DISABLED = "未启用：没有配置 extensions.alert-source"


@dataclass(frozen=True)
class AlertsDependencies:
    config: ProjectConfig
    client: ExtensionClient
    redactor: ProbeRedactor
    release_at: ReleaseAt
    randomness: RandomBytes = os.urandom


def excluded(alert: Mapping[str, Any], names: Sequence[str], labels: Mapping[str, Sequence[str]]) -> bool:
    if any(re.search(pattern, alert["name"]) for pattern in names):
        return True
    return any(alert["labels"].get(key) in values for key, values in labels.items())


def message(alert: Mapping[str, Any]) -> str:
    annotations = alert["annotations"]
    return next((annotations[key] for key in MESSAGE_KEYS if annotations.get(key)), alert["name"])


class AlertsSource:
    name = ProbeKind.ALERTS
    levels = PROBE_LEVELS[ProbeKind.ALERTS]

    def __init__(self, dependencies: AlertsDependencies) -> None:
        self.deps = dependencies

    def run(self, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome:
        if not self.deps.client.configured(ExtensionPoint.ALERT_SOURCE):
            return skipped(DISABLED)
        result = self.deps.client.alert_source()
        extensions = {result.point.value: result.stats_entry()}
        if result.output is None:
            return failed(failure_text(result), *result.notes, extensions=extensions)
        exclude = self.deps.config.get("sources.alerts.exclude")
        names, labels = list(exclude.get("names", [])), dict(exclude.get("labels", {}))
        alerts = result.output["alerts"]
        kept = [alert for alert in alerts if not excluded(alert, names, labels)]
        factory = SignalFactory(target, self.name, self.deps.redactor, randomness=self.deps.randomness)
        signals = []
        for alert in kept:
            started = parse_iso(alert["startsAt"])
            signals.append(factory.create(
                source=Source.BEHAVIOR, check=CHECK, location=alert["name"], message=message(alert),
                occurred_at=started, release=self.deps.release_at(started),
                context={PLATFORM_GROUP: f"{GROUP_PREFIX}{alert['fingerprint']}", "sourceName": SOURCE_NAME,
                         "severityHint": alert["labels"].get("severity"), "labels": dict(alert["labels"]),
                         "annotations": dict(alert["annotations"]), "generatorUrl": alert.get("generatorUrl")}))
        stats = {"alerts": len(alerts), "excludedAlerts": len(alerts) - len(kept), "signals": len(signals)}
        return ProbeOutcome(RunStatus.OK, tuple(signals), Coverage(sources=(SOURCE_NAME,)), stats=stats,
                            notes=tuple(result.notes), extensions=extensions)
