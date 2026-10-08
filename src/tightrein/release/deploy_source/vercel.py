"""vercel：Vercel 的部署列表(REST API `GET /v6/deployments`，只读)。

- 参数 projectId、target、limit，有 teamId 时带上；令牌(secrets.json 的 vercel.token)只放进请求头，不进输出、日志与
  错误信息；
- 没有 Git commit 的部署(手动上传)不计入；commit 取 meta 中 github、gitlab、bitbucket 的 commit 字段；
  created 是毫秒时间戳；READY 为 succeeded，ERROR、CANCELED 为 failed，其余为 running。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from tightrein.protocol.http import HttpRequest, Transport
from tightrein.protocol.methods import Configured
from tightrein.release.deploy import (
    FAILED,
    RUNNING,
    SUCCEEDED,
    DeployError,
    DeployErrorKind,
    DeployRecord,
    SourceContext,
    sorted_records,
)

API = "https://api.vercel.com/v6/deployments"
READY = "READY"
FAILED_STATES = frozenset({"ERROR", "CANCELED"})
COMMIT_KEYS = ("githubCommitSha", "gitlabCommitSha", "bitbucketCommitSha")
MILLISECONDS_PER_SECOND = 1000


def read(configured: Configured, context: SourceContext) -> list[DeployRecord]:
    options = configured.options
    assert configured.token is not None  # 清单 secretRequired：缺凭据时 methods.configure 已报错
    return vercel(context.transport, token=configured.token, project_id=str(options["projectId"]),
                  target=str(options["target"]), team_id=options.get("teamId"), limit=int(options["limit"]),
                  timeout_s=context.timeout_s)


def vercel(transport: Transport, *, token: str, project_id: str, target: str, team_id: str | None, limit: int,
           timeout_s: float) -> list[DeployRecord]:
    query: dict[str, Any] = {"projectId": project_id, "target": target, "limit": limit,
                             **({"teamId": team_id} if team_id else {})}
    response = transport(HttpRequest("GET", f"{API}?{urlencode(query)}", timeout_s,
                                     {"Authorization": f"Bearer {token}", "Accept": "application/json"}))
    if response.status is None:
        raise DeployError(DeployErrorKind.UNAVAILABLE, f"无法访问 Vercel API：{response.error}")
    if not response.ok:
        raise DeployError(DeployErrorKind.UNAVAILABLE,
                          f"Vercel API 返回 {response.status}：确认令牌有效、有该项目的读取权限，projectId 与 teamId 正确")
    try:
        found = json.loads(response.text()).get("deployments") or []
    except (ValueError, AttributeError) as error:
        raise DeployError(DeployErrorKind.INVALID, "Vercel API 的响应不是预期的 JSON") from error
    records = []
    for item in found:
        meta = item.get("meta") or {}
        commit = next((meta[key] for key in COMMIT_KEYS if meta.get(key)), None)
        if commit is None:
            continue
        created = datetime.fromtimestamp(int(item["created"]) / MILLISECONDS_PER_SECOND, tz=UTC)
        records.append(DeployRecord(
            item["uid"], commit, vercel_status(item.get("state") or item.get("readyState")), item.get("target"),
            f"https://{item['url']}" if item.get("url") else None, created.replace(microsecond=0),
        ))
    return sorted_records(records)


def vercel_status(value: str | None) -> str:
    if value == READY:
        return SUCCEEDED
    return FAILED if value in FAILED_STATES else RUNNING
