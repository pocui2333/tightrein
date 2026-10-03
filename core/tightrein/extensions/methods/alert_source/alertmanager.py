"""core/alertmanager：经 Alertmanager API v2 读取已触发、未静默的告警(redesign/01-collect.md 第 4 节)。

GET <url>/api/v2/alerts?active=true&silenced=false&inhibited=false。同一个接口适用于 Prometheus Alertmanager、
Grafana 内置的 Alertmanager(url 写 <Grafana 地址>/api/alertmanager/grafana)、Grafana Cloud 与 Mimir。
告警自带的 fingerprint 作为平台分组编号；基础设施类告警的排除由核心按 sources.alerts.exclude 完成。
认证：有 user 时为基本认证，否则为 Bearer；keychainItem 为空时不带认证。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import platforms, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
PLATFORM = "Alertmanager"
NAME_LABEL = "alertname"


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    options = request.options
    headers = platforms.auth_headers(platforms.token(context, options["keychainItem"]), options["user"])
    found = platforms.get_json(context, options["url"].rstrip("/") + "/api/v2/alerts",
                               {"active": "true", "silenced": "false", "inhibited": "false"}, headers,
                               float(options["timeoutSeconds"]), PLATFORM)
    if not isinstance(found, list):
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, "Alertmanager 的响应不是告警数组")
    alerts = []
    for item in found:
        labels = {str(key): str(value) for key, value in (item.get("labels") or {}).items()}
        fingerprint = str(item["fingerprint"])
        alerts.append({"fingerprint": fingerprint, "name": labels.get(NAME_LABEL) or fingerprint,
                       "labels": labels,
                       "annotations": {str(key): str(value) for key, value in (item.get("annotations") or {}).items()},
                       "startsAt": platforms.utc(item["startsAt"]), "generatorUrl": item.get("generatorURL") or None})
    return MethodResult({"alerts": alerts})


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
