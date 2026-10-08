"""业务告警的采集流程：读取已触发、未静默的告警，排除基础设施类，其余每条告警一条信号。只收集监控系统已经报出的，
自己不判断。

- 基础设施类(磁盘、内存、重启等)按 controls."collect.alerts".exclude 排除：告警名匹配 names 中任一正则，或某个
  标签的取值在 labels 给出的清单中；
- 信号：check_type 为 alert，location 为告警名，message 依次取注解 summary、description、message，都没有时用告警名；
  occurred_at 为告警开始时间；告警自带的 fingerprint 作为平台分组编号，指纹写成 `alertmanager:<fingerprint>`；
- 不按时间窗口读(只读当前在触发的)，没有读取位置。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from tightrein.collect.alerts.alert_source.alertmanager import Alert
from tightrein.collect.common.signals import factory_for, releases
from tightrein.collect.common.source import SourceMisconfigured, SourceResult, SourceStatus, skipped
from tightrein.protocol import methods
from tightrein.protocol.handoff import Metrics
from tightrein.protocol.http import Transport, UrllibTransport

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

SOURCE = "collect.alerts"
METHODS = "tightrein.collect.alerts.alert_source"
CHECK_TYPE = "alert"
MESSAGE_KEYS = ("summary", "description", "message")


def collect(runtime: Runtime, transport: Transport | None = None) -> SourceResult:
    """可能抛 SourceError(平台不可用、配置不对)，由调用方经 common.source.guarded 归类。"""
    module = runtime.setup.module(SOURCE)
    if not runtime.setup.enabled(SOURCE):
        return skipped(SOURCE, f"未启用：{module.reason or '接入清单中为 disabled'}")
    if not module.method or not methods.exists(METHODS, module.method):
        raise SourceMisconfigured(f"setup.json 中 collect.alerts 的 method 不是已有的告警来源：{module.method}")
    method = methods.load(METHODS, module.method)
    configured = methods.configure(method, settings=runtime.settings, source=SOURCE, secrets=runtime.secrets)
    alerts: list[Alert] = method.module.read(configured, transport=transport or UrllibTransport(),
                                             timeout_s=runtime.settings.duration("limits.timeouts.http"))
    exclude = runtime.settings.section(SOURCE)["exclude"]
    names = [re.compile(pattern) for pattern in exclude.get("names", [])]
    labels: dict[str, list[str]] = dict(exclude.get("labels", {}))
    kept = [alert for alert in alerts if not excluded(alert, names, labels)]
    factory = factory_for(runtime, SOURCE)
    release_at = releases(runtime.conn)
    signals = [factory.create(
        check_type=CHECK_TYPE, location=alert.name, message=message(alert), occurred_at=alert.starts_at,
        commit=release_at(alert.starts_at), group_key=f"{module.method}:{alert.fingerprint}",
        evidence={"sourceName": module.method, "severity": alert.labels.get("severity"), "labels": alert.labels,
                  "annotations": alert.annotations, "generatorUrl": alert.generator_url})
        for alert in kept]
    produced = {"alerts": len(alerts), "excludedAlerts": len(alerts) - len(kept), "signals": len(signals)}
    return SourceResult(SOURCE, SourceStatus.DONE, signals, len(alerts), None, None, {}, Metrics(produced=produced),
                        coverage=[module.method])


def excluded(alert: Alert, names: Sequence[re.Pattern[str]], labels: Mapping[str, Sequence[str]]) -> bool:
    if any(pattern.search(alert.name) for pattern in names):
        return True
    return any(alert.labels.get(key) in values for key, values in labels.items())


def message(alert: Alert) -> str:
    return next((alert.annotations[key] for key in MESSAGE_KEYS if alert.annotations.get(key)), alert.name)
