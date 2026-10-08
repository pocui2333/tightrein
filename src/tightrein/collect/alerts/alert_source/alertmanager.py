"""alertmanager：经 Alertmanager API v2 读取已触发、未静默、未被抑制的告警(只读 API)。

GET <url>/api/v2/alerts?active=true&silenced=false&inhibited=false。同一个接口适用于 Prometheus Alertmanager、
Grafana 内置的 Alertmanager(url 写 <Grafana 地址>/api/alertmanager/grafana)、Grafana Cloud 与 Mimir。
告警自带的 fingerprint 作为平台分组编号。认证：有 user 时为基本认证，否则为 Bearer；没有令牌时不带认证。
平台给的开始时间带 9 位纳秒，先去掉秒以下再换成 UTC。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tightrein.collect.common.source import SourceInvalid
from tightrein.collect.common.window import platform_time
from tightrein.protocol.http import Platform, Transport, auth_headers
from tightrein.protocol.methods import Configured

PLATFORM = "Alertmanager"
ALERTS_PATH = "/api/v2/alerts"
NAME_LABEL = "alertname"
FIRING = {"active": "true", "silenced": "false", "inhibited": "false"}


@dataclass(frozen=True)
class Alert:
    fingerprint: str
    name: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime
    generator_url: str | None


def read(configured: Configured, *, transport: Transport, timeout_s: float) -> list[Alert]:
    options = configured.options
    platform = Platform(PLATFORM, transport, timeout_s, auth_headers(configured.token, options.get("user")))
    found = platform.get_json(options["url"].rstrip("/") + ALERTS_PATH, FIRING)
    if not isinstance(found, list):
        raise SourceInvalid("Alertmanager 的响应不是告警数组")
    alerts = []
    for item in found:
        labels = {str(key): str(value) for key, value in (item.get("labels") or {}).items()}
        try:
            fingerprint, starts_at = str(item["fingerprint"]), platform_time(item["startsAt"])
        except (KeyError, ValueError) as error:
            raise SourceInvalid("Alertmanager 的告警缺少 fingerprint 或开始时间认不出") from error
        alerts.append(Alert(fingerprint, labels.get(NAME_LABEL) or fingerprint, labels,
                            {str(key): str(value) for key, value in (item.get("annotations") or {}).items()},
                            starts_at, item.get("generatorURL") or None))
    return alerts
