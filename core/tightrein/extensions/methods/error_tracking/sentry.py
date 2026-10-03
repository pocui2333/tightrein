"""core/sentry：读取 Sentry(或兼容其 API 的 GlitchTip)中时间窗口内有新事件的错误分组(redesign/01-collect.md 第 1 节)。

- 分组列表：GET <baseUrl>/api/0/organizations/<organization>/issues/，参数 start、end(窗口)、query=is:unresolved、
  sort=date、limit、project(可多个)、environment；按响应头 Link 中 rel="next"; results="true" 的 cursor 翻页，
  读满 options.limit 条仍有下一页时 truncated 为真。
- 每个分组再取最新事件 .../issues/<编号>/events/latest/：异常的堆栈帧(entries 中 type 为 exception 的最后一个值，
  Sentry 的帧从外到内排列，输出时出错处在前)、操作轨迹(breadcrumbs，取最后 options.breadcrumbs 条)、标签中的
  url 与 browser、release。
- 分组编号写成 sentry:<organization>/<编号>，作为问题指纹；platform 为 javascript 系的分组记为前端错误。
- 令牌(只需 event:read)从 options.keychainItem 指定的钥匙串条目读取；oldestAvailable 按 retentionDays 估算。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tightrein.extensions.methods import platforms, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
PLATFORM = "Sentry"
PAGE_SIZE = 100
FRONTEND_PLATFORMS = ("javascript", "node-javascript-browser")
NEXT_LINK = re.compile(r'<[^>]*>;\s*rel="next";\s*results="true";\s*cursor="(?P<cursor>[^"]+)"')


def next_cursor(link: str | None) -> str | None:
    if not link:
        return None
    found = NEXT_LINK.search(link)
    return found.group("cursor") if found else None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name), None)


def _entry(event: Mapping[str, Any], kind: str) -> Any:
    return next((item.get("data") for item in event.get("entries") or [] if item.get("type") == kind), None)


def frames(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = _entry(event, "exception") or {}
    values = data.get("values") or []
    stack = (values[-1].get("stacktrace") or {}) if values else {}
    found = [{"file": frame.get("filename") or frame.get("absPath"), "function": frame.get("function"),
              "line": frame.get("lineNo"), "inApp": bool(frame.get("inApp"))} for frame in stack.get("frames") or []]
    return list(reversed(found))


def breadcrumbs(event: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
    data = _entry(event, "breadcrumbs") or {}
    values = (data.get("values") or [])[-limit:] if limit else []
    return [{"time": item.get("timestamp"), "category": item.get("category"), "message": item.get("message")}
            for item in values]


def _tag(event: Mapping[str, Any], key: str) -> str | None:
    return next((item.get("value") for item in event.get("tags") or [] if item.get("key") == key), None)


def _release(event: Mapping[str, Any]) -> str | None:
    release = event.get("release")
    if isinstance(release, Mapping):
        return release.get("version")
    return release if isinstance(release, str) else None


def to_issue(organization: str, issue: Mapping[str, Any], event: Mapping[str, Any],
             crumbs: int) -> dict[str, Any]:
    metadata = issue.get("metadata") or {}
    platform = str(event.get("platform") or issue.get("platform") or "")
    return {
        "group": f"sentry:{organization}/{issue['id']}",
        "kind": "frontend" if platform.startswith(FRONTEND_PLATFORMS) else "backend",
        "title": issue.get("title") or metadata.get("type") or str(issue["id"]),
        "type": metadata.get("type"),
        "message": metadata.get("value"),
        "culprit": issue.get("culprit") or None,
        "level": issue.get("level"),
        "count": int(issue.get("count") or 0),
        "userCount": int(issue.get("userCount") or 0),
        "firstSeen": platforms.utc(issue["firstSeen"]),
        "lastSeen": platforms.utc(issue["lastSeen"]),
        "release": _release(event),
        "environment": _tag(event, "environment"),
        "permalink": issue.get("permalink"),
        "frames": frames(event),
        "breadcrumbs": breadcrumbs(event, crumbs),
        "url": _tag(event, "url"),
        "browser": _tag(event, "browser"),
    }


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    options = request.options
    base = options["baseUrl"].rstrip("/")
    organization = options["organization"]
    headers = platforms.auth_headers(platforms.token(context, options["keychainItem"]))
    timeout = float(options["timeoutSeconds"])
    query: list[tuple[str, Any]] = [("start", request.input["since"]), ("end", request.input["until"]),
                                    ("query", "is:unresolved"), ("sort", "date"), ("limit", PAGE_SIZE)]
    query += [("project", project) for project in options["projects"]]
    if options["environment"]:
        query.append(("environment", options["environment"]))
    url = f"{base}/api/0/organizations/{organization}/issues/"
    found: list[Mapping[str, Any]] = []
    truncated = False
    cursor: str | None = None
    while True:
        response = platforms.get(context, url, [*query, *([("cursor", cursor)] if cursor else [])], headers,
                                 timeout, PLATFORM)
        page = platforms.parse_json(response, PLATFORM)
        found += page
        cursor = next_cursor(_header(response.headers, "link"))
        if len(found) >= options["limit"]:
            truncated = len(found) > options["limit"] or cursor is not None
            found = found[:options["limit"]]
            break
        if cursor is None:
            break
    issues = []
    for issue in found:
        event = platforms.get_json(context, f"{url}{issue['id']}/events/latest/", None, headers, timeout, PLATFORM)
        issues.append(to_issue(organization, issue, event, int(options["breadcrumbs"])))
    oldest = platforms.oldest_available(context.now(), int(options["retentionDays"]))
    return MethodResult({"issues": issues, "truncated": truncated, "oldestAvailable": oldest})


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
