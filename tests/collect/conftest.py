"""采集来源测试共用：真实数据库与缺省配置，接入清单、站点、凭据按需给出；HTTP 用按路径应答的假传输，不联网。"""

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tightrein.onboard.setup import MODULES, ModuleSetup, ModuleStatus, Setup
from tightrein.protocol.http import HttpRequest, HttpResponse
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.security import Redactor
from tightrein.settings.load import Layer, Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout

SOURCE_NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
SOURCE_RUN = "R-20261005T030000Z-collect"
DEFAULTS = Path(__file__).resolve().parents[2] / "settings" / "defaults.json"


class RouteTransport:
    """按 URL 路径(与可选的查询条件)返回预设响应，记录全部请求；没有匹配的返回 404。"""

    def __init__(self, routes: list[tuple[str, Mapping[str, str], HttpResponse]]) -> None:
        self.routes = routes
        self.sent: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        parts = urlsplit(request.url)
        query = parse_qs(parts.query)
        for path, match, response in self.routes:
            if parts.path == path and all(query.get(key) == [value] for key, value in match.items()):
                return response
        return HttpResponse(404, b"{}")

    def query(self, index: int) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.sent[index].url).query)


def json_response(body: Any, headers: Mapping[str, str] | None = None, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(body).encode("utf-8"), dict(headers or {}))


@pytest.fixture
def source_clock() -> FixedClock:
    return FixedClock(SOURCE_NOW)


@pytest.fixture
def source_layout(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(tmp_path / "workspaces" / "demo")


@pytest.fixture
def source_conn(source_layout: WorkspaceLayout, source_clock: FixedClock) -> Iterator[sqlite3.Connection]:
    connection = open_database(source_layout.database, clock=source_clock)
    yield connection
    connection.close()


@pytest.fixture
def routes() -> type[RouteTransport]:
    return RouteTransport


@pytest.fixture
def respond() -> Callable[..., HttpResponse]:
    return json_response


@pytest.fixture
def source_runtime(source_conn: sqlite3.Connection, source_clock: FixedClock,
                   source_layout: WorkspaceLayout) -> Callable[..., SimpleNamespace]:
    """make(modules={键: {status, method, script, secrets}}, controls={控制键: {...}}, sites=, secrets=, runner=)。
    没给出的模块为 disabled。"""

    def make(*, modules: Mapping[str, Mapping[str, Any]] | None = None,
             controls: Mapping[str, Mapping[str, Any]] | None = None, sites: Mapping[str, Any] | None = None,
             secrets: Mapping[str, str] | None = None, runner: Any = None,
             environ: Mapping[str, str] | None = None) -> SimpleNamespace:
        defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
        layers = [Layer("defaults", None, defaults), Layer("test", None, {"controls": dict(controls or {})})]
        settings = Settings(layers, None, dict(sites or {}))
        redactor = Redactor()
        for value in (secrets or {}).values():
            redactor.register(value)
        source_layout.root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(conn=source_conn, clock=source_clock, workspace=source_layout, run=SOURCE_RUN,
                               settings=settings, setup=make_setup(modules or {}), secrets=dict(secrets or {}),
                               redactor=redactor, runner=runner, environ=dict(environ or {"PATH": "/usr/bin:/bin"}))

    return make


def make_setup(modules: Mapping[str, Mapping[str, Any]]) -> Setup:
    found = {}
    for key in MODULES:
        given = modules.get(key)
        if given is None:
            found[key] = ModuleSetup(key, ModuleStatus.DISABLED, None, None, None, "测试中不启用", None)
        else:
            found[key] = ModuleSetup(key, ModuleStatus(given.get("status", "enabled")), given.get("method"),
                                     given.get("script"), given.get("guide"), given.get("reason"), None,
                                     tuple(given.get("secrets") or ()))
    return Setup("demo", "2026-10-05T00:00:00Z", found)
