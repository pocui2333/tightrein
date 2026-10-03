"""用例结果中的页面失败：用例失败、控制台报错与失败请求，按发生的页面路径记录。

| 情况 | 记录 |
|---|---|
| outcome 为 unexpected | 一条 case-failure，页面为失败时所在页面(最后一次尝试最后访问的页面) |
| 控制台报错(不论用例结果，各次尝试) | console-error，同一用例、同一页面、同一原文只保留一条 |
| 失败请求(不论用例结果，各次尝试) | failed-request，页面为发起请求的页面，同一用例、同一请求、同一结果只保留一条 |

setup 项目的用例不产出失败：失败的角色记入 failed_setups。flaky 的用例不算失败。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from tightrein.pipeline.checks.pages.result_parser import CaseResult, Results
from tightrein.sources.common.routes import clean_path

CASE_FAILURE = "case-failure"
CONSOLE_ERROR = "console-error"
FAILED_REQUEST = "failed-request"
WEB_SCHEMES = ("http", "https")
ROOT = "/"


@dataclass(frozen=True)
class PageFailure:
    page: str
    kind: str
    message: str
    case: str


@dataclass(frozen=True)
class PageFailures:
    failures: tuple[PageFailure, ...]
    failed_setups: Mapping[str, str] = field(default_factory=dict)
    executed_roles: frozenset[str] = frozenset()


def page_path(url: str) -> str | None:
    """页面 URL 的路径；about:blank 等非网页地址为空。"""
    return clean_path(url) if urlsplit(url).scheme in WEB_SCHEMES else None


def _last_page(case: CaseResult) -> str:
    for observation in reversed(case.observations):
        for url in reversed(observation.get("pages") or []):
            path = page_path(url)
            if path is not None:
                return path
    return ROOT


def collect(results: Results) -> PageFailures:
    found: dict[tuple[str, str, str, str], PageFailure] = {}
    failed_setups: dict[str, str] = {}
    executed: set[str] = set()

    def add(case: CaseResult, page: str, kind: str, message: str) -> None:
        key = (case.title, page, kind, message)
        found.setdefault(key, PageFailure(page, kind, message, case.title))

    for case in results.cases:
        if case.setup:
            if case.outcome == "unexpected":
                failed_setups[case.role] = case.error or "登录失败"
            continue
        if case.outcome == "skipped":
            continue
        executed.add(case.role)
        if case.outcome == "unexpected":
            add(case, _last_page(case), CASE_FAILURE, case.error or f"用例失败：{case.title}")
        for observation in case.observations:
            for item in observation.get("consoleErrors") or []:
                add(case, page_path(item.get("pageUrl") or "") or ROOT, CONSOLE_ERROR, item.get("text") or "控制台报错")
            for item in observation.get("failedRequests") or []:
                method = (item.get("method") or "GET").upper()
                outcome = str(item["status"]) if item.get("status") is not None else (item.get("failure") or "请求失败")
                add(case, page_path(item.get("pageUrl") or "") or ROOT, FAILED_REQUEST,
                    f"{outcome} {method} {clean_path(item.get('url') or '')}")
    return PageFailures(tuple(found.values()), failed_setups, frozenset(executed))
