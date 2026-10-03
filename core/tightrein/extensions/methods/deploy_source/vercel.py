"""core/vercel：Vercel 项目的部署记录(redesign/07-release.md 第 4 节)。

GET https://api.vercel.com/v6/deployments?projectId=..&target=..&limit=..(团队项目加 teamId)，请求头
`Authorization: Bearer <令牌>`；令牌在方法进程内经 config/secrets.Keychain 从 options.keychainItem 读取，只用于请求头，
不写进输出、日志与错误信息(扩展进程的环境变量会滤掉凭证，因此不经环境变量传入)。状态：READY 为 succeeded，
ERROR、CANCELED 为 failed，其余(QUEUED、INITIALIZING、BUILDING)为 running；没有 Git commit 的部署(手动上传)不计入。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from tightrein.config.secrets import SecretError
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import deploys, platforms, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult
from tightrein.sources.common.http import HttpRequest

MANIFEST = Path(__file__).with_suffix(".yaml")
API = "https://api.vercel.com/v6/deployments"
READY = "READY"
FAILED_STATES = frozenset({"ERROR", "CANCELED"})
COMMIT_KEYS = ("githubCommitSha", "gitlabCommitSha", "bitbucketCommitSha")
MILLISECONDS = 1000


def status_of(state: str | None) -> str:
    if state == READY:
        return deploys.SUCCEEDED
    return deploys.FAILED if state in FAILED_STATES else deploys.RUNNING


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    options = request.options
    try:
        token = platforms.keychain(context).read(options["keychainItem"]).value
    except SecretError as error:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, str(error),
                          "用 security add-generic-password -s <条目名> -a vercel -w 存放 Vercel 访问令牌") from error
    query = {"projectId": options["projectId"], "target": options["target"], "limit": options["limit"],
             **({"teamId": options["teamId"]} if options["teamId"] else {})}
    response = context.transport(HttpRequest("GET", f"{API}?{urlencode(query)}",
                                             {"Authorization": f"Bearer {token}", "Accept": "application/json"}, None,
                                             float(options["timeoutSeconds"])))
    if response.status is None:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, f"无法访问 Vercel API：{response.error}")
    if not response.ok:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, f"Vercel API 返回 {response.status}",
                          "确认令牌有效、有该项目的读取权限，projectId 与 teamId 正确")
    try:
        found = json.loads(response.text()).get("deployments") or []
    except (ValueError, AttributeError) as error:
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, "Vercel API 的响应不是预期的 JSON") from error
    records = []
    for item in found:
        meta = item.get("meta") or {}
        commit = next((meta[key] for key in COMMIT_KEYS if meta.get(key)), None)
        if commit is None:
            continue
        created = datetime.fromtimestamp(int(item["created"]) / MILLISECONDS, tz=timezone.utc)
        url = f"https://{item['url']}" if item.get("url") else None
        records.append(deploys.record(item["uid"], commit, status_of(item.get("state") or item.get("readyState")),
                                      item.get("target"), url, format_iso(created)))
    return MethodResult(deploys.output(records))


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
