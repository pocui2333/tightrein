"""sentry：读取 Sentry(或兼容其 API 的 GlitchTip)中时间窗口内有新事件的错误分组(只读 API)。

- 分组列表：GET <url>/api/0/organizations/<organization>/issues/，参数 start、end、query=is:unresolved、sort=date、
  limit、project(可多个)、environment；按响应头 Link 中 `rel="next"; results="true"` 的 cursor 翻页
  (`results="false"` 也带 cursor，不能只看有没有 cursor)；读满 limit 仍有下一页时 truncated 为真；
- 每个分组再取最新事件 .../issues/<编号>/events/latest/：堆栈取 exception 条目的最后一个值(链式异常的最外层)，
  Sentry 的帧从外到内排列，输出时倒过来让出错处在前；操作轨迹只取最后 breadcrumbs 条；标签中的 url、browser、
  environment 与 release；
- 分组编号写成 `sentry:<organization>/<编号>`，作为问题指纹；platform 以 javascript 开头的记为前端；
- 令牌只放进请求头；oldestAvailable 按 retentionDays 估算。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tightrein.collect.common.source import SourceInvalid
from tightrein.collect.common.window import oldest_available, platform_time
from tightrein.protocol.http import HttpResponse, Platform, Transport, auth_headers, parse_json
from tightrein.protocol.methods import Configured
from tightrein.protocol.naming import format_iso

PLATFORM = "Sentry"
PAGE_SIZE = 100
FRONTEND_PLATFORM = "javascript"
NEXT_LINK = re.compile(r'<[^>]*>;\s*rel="next";\s*results="true";\s*cursor="(?P<cursor>[^"]+)"')


@dataclass(frozen=True)
class StackFrame:
    file: str | None
    function: str | None
    line: int | None
    in_app: bool


@dataclass(frozen=True)
class Breadcrumb:
    time: str | None
    category: str | None
    message: str | None


@dataclass(frozen=True)
class TrackedIssue:
    group: str  # sentry:<组织>/<编号>
    frontend: bool
    title: str
    type: str | None
    message: str | None
    culprit: str | None
    level: str | None
    count: int
    user_count: int
    first_seen: datetime
    last_seen: datetime
    release: str | None  # 平台上的版本号
    environment: str | None
    permalink: str | None
    frames: tuple[StackFrame, ...]  # 出错处在前
    breadcrumbs: tuple[Breadcrumb, ...]
    url: str | None
    browser: str | None


@dataclass(frozen=True)
class TrackingRead:
    issues: list[TrackedIssue]
    truncated: bool
    oldest_available: datetime


def read(configured: Configured, *, transport: Transport, timeout_s: float, since: datetime, until: datetime,
         now: datetime) -> TrackingRead:
    options = configured.options
    platform = Platform(PLATFORM, transport, timeout_s, auth_headers(configured.token))
    organization = options["organization"]
    url = f"{options['url'].rstrip('/')}/api/0/organizations/{organization}/issues/"
    query: list[tuple[str, Any]] = [("start", format_iso(since)), ("end", format_iso(until)),
                                    ("query", "is:unresolved"), ("sort", "date"), ("limit", PAGE_SIZE)]
    query += [("project", project) for project in options["projects"]]
    if options.get("environment"):
        query.append(("environment", options["environment"]))
    limit = int(options["limit"])
    found: list[Mapping[str, Any]] = []
    truncated = False
    cursor: str | None = None
    while True:
        response = platform.get(url, [*query, *([("cursor", cursor)] if cursor else [])])
        page = _list(platform, response)
        found += page
        cursor = next_cursor(response.header("link"))
        if len(found) >= limit:
            truncated = len(found) > limit or cursor is not None
            found = found[:limit]
            break
        if cursor is None:
            break
    issues = [to_issue(organization, item, platform.get_json(f"{url}{item['id']}/events/latest/"),
                       int(options["breadcrumbs"])) for item in found]
    return TrackingRead(issues, truncated, oldest_available(now, int(options["retentionDays"])))


def next_cursor(link: str | None) -> str | None:
    if not link:
        return None
    found = NEXT_LINK.search(link)
    return found.group("cursor") if found else None


def to_issue(organization: str, issue: Mapping[str, Any], event: Mapping[str, Any], crumbs: int) -> TrackedIssue:
    metadata = issue.get("metadata") or {}
    platform = str(event.get("platform") or issue.get("platform") or "")
    try:
        first_seen, last_seen = platform_time(issue["firstSeen"]), platform_time(issue["lastSeen"])
    except (KeyError, ValueError) as error:
        raise SourceInvalid(f"Sentry 分组 {issue.get('id')} 的时间认不出") from error
    return TrackedIssue(
        group=f"sentry:{organization}/{issue['id']}",
        frontend=platform.startswith(FRONTEND_PLATFORM),
        title=issue.get("title") or metadata.get("type") or str(issue["id"]),
        type=metadata.get("type"),
        message=metadata.get("value"),
        culprit=issue.get("culprit") or None,
        level=issue.get("level"),
        count=int(issue.get("count") or 0),
        user_count=int(issue.get("userCount") or 0),
        first_seen=first_seen,
        last_seen=last_seen,
        release=_release(event),
        environment=_tag(event, "environment"),
        permalink=issue.get("permalink"),
        frames=frames(event),
        breadcrumbs=breadcrumbs(event, crumbs),
        url=_tag(event, "url"),
        browser=_tag(event, "browser"),
    )


def frames(event: Mapping[str, Any]) -> tuple[StackFrame, ...]:
    values = (_entry(event, "exception") or {}).get("values") or []
    stack = (values[-1].get("stacktrace") or {}) if values else {}
    found = [StackFrame(frame.get("filename") or frame.get("absPath"), frame.get("function"), frame.get("lineNo"),
                        bool(frame.get("inApp"))) for frame in stack.get("frames") or []]
    return tuple(reversed(found))


def breadcrumbs(event: Mapping[str, Any], limit: int) -> tuple[Breadcrumb, ...]:
    values = ((_entry(event, "breadcrumbs") or {}).get("values") or [])[-limit:] if limit else []
    return tuple(Breadcrumb(item.get("timestamp"), item.get("category"), item.get("message")) for item in values)


def _list(platform: Platform, response: HttpResponse) -> list[Mapping[str, Any]]:
    page = parse_json(response, platform.name)
    if not isinstance(page, list):
        raise SourceInvalid("Sentry 的分组列表不是数组")
    return page


def _entry(event: Mapping[str, Any], kind: str) -> Any:
    return next((item.get("data") for item in event.get("entries") or [] if item.get("type") == kind), None)


def _tag(event: Mapping[str, Any], key: str) -> str | None:
    return next((item.get("value") for item in event.get("tags") or [] if item.get("key") == key), None)


def _release(event: Mapping[str, Any]) -> str | None:
    release = event.get("release")
    if isinstance(release, Mapping):
        return release.get("version")
    return release if isinstance(release, str) else None
