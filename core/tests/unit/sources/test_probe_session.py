import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from probe_world import make_redactor

from tightrein.config.secrets import SecretNotFound
from tightrein.sources.common.http import HttpRequest, HttpResponse, UrllibTransport
from tightrein.sources.common.session import (ANONYMOUS_ROLE, NO_ACCOUNTS, NO_TARGET, KeychainCredentials, LoginFailed,
                                              LoginSettings, RoleToken, Session, extract, fill, probe_roles)

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiQ29tcGFueSJ9.c2lnbmF0dXJl"
SETTINGS = LoginSettings("/api/Auth/Login", {"userName": "{account}", "password": "{password}", "remember": True},
                         "data.token")


class FakeCredentials:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def account(self, role):
        if role in self.missing:
            raise SecretNotFound(f"tightrein.demo.{role.lower()}", "条目不存在")
        return f"test-{role.lower()}"

    def password(self, role):
        return f"pw-{role}"


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        body = json.loads(request.body)
        return self.responses[body["userName"]]


def ok(token=TOKEN):
    return HttpResponse(200, json.dumps({"data": {"token": token}}).encode())


def session(responses, missing=()):
    redactor = make_redactor()
    return Session("https://staging.example.test/", SETTINGS, FakeCredentials(missing), FakeTransport(responses),
                   redactor), redactor


def test_template_filling_and_token_path():
    assert fill({"a": "{account}", "b": ["{password}", 1], "c": "x-{account}"}, "u", "p") == {
        "a": "u", "b": ["p", 1], "c": "x-u"}
    assert extract({"data": {"token": "t"}}, "data.token") == "t"
    assert extract({"data": []}, "data.token") is None


def test_login_posts_the_filled_template_and_registers_the_token():
    current, redactor = session({"test-company": ok()})
    token = current.login("Company")
    assert token == RoleToken("Company", "test-company", TOKEN)
    request = current.transport.requests[0]
    assert (request.method, request.url) == ("POST", "https://staging.example.test/api/Auth/Login")
    assert json.loads(request.body) == {"userName": "test-company", "password": "pw-Company", "remember": True}
    assert TOKEN not in repr(token)
    assert redactor.text(f"Authorization {TOKEN}") == "Authorization [已脱敏]"
    assert current.login("Company") is token and len(current.transport.requests) == 1
    assert current.tokens() == (TOKEN,)


@pytest.mark.parametrize(("response", "reason"), [
    (HttpResponse(None, error="URLError: refused"), "登录接口没有响应：URLError: refused"),
    (HttpResponse(401, b'{"password": "pw-Company"}'), "登录接口返回 401"),
    (HttpResponse(200, b"<html>"), "登录接口的响应不是 JSON"),
    (HttpResponse(200, b'{"data": {"token": ""}}'), "登录响应中 data.token 不是非空字符串"),
])
def test_login_failures(response, reason):
    current, _ = session({"test-company": response})
    with pytest.raises(LoginFailed) as caught:
        current.login("Company")
    assert caught.value.reason == reason and "pw-Company" not in str(caught.value)


def test_login_all_separates_failed_roles():
    current, _ = session({"test-company": ok(), "test-personal": HttpResponse(403)}, missing={"Admin"})
    tokens, failures = current.login_all(["Company", "Personal", "Admin"])
    assert list(tokens) == ["Company"]
    assert failures["Personal"] == "登录接口返回 403"
    assert failures["Admin"].startswith("钥匙串条目 tightrein.demo.admin")


def test_without_accounts_or_a_target_login_fails_with_the_reason(make_config):
    assert LoginSettings.from_config(make_config(accounts=None)) is None
    transport = FakeTransport({})
    unconfigured = Session("https://staging.example.test", None, FakeCredentials(), transport, make_redactor())
    no_target = Session(None, SETTINGS, FakeCredentials(), transport, make_redactor())
    assert unconfigured.login_all(["Company"]) == ({}, {"Company": NO_ACCOUNTS})
    assert no_target.login_all(["Company"]) == ({}, {"Company": NO_TARGET})
    assert transport.requests == []


ACCESS_CODE = "code-7f3a9c41"
STATIC_LOGIN = {"kind": "static-header", "header": "X-Access-Code"}
VERIFY = {"endpoint": "/api/login", "bodyTemplate": {"code": "{password}", "who": "{account}"}}


class CodeCredentials:
    def account(self, role):
        return role

    def password(self, role):
        return ACCESS_CODE


def static_session(make_config, login, responses=None, base_url="https://site.example.test"):
    config = make_config(accounts={"roles": {"Learner": {"keychain": "tightrein.demo.learner"}}, "login": login})
    transport = FakeResponder(responses or {})
    redactor = make_redactor()
    return Session(base_url, LoginSettings.from_config(config), CodeCredentials(), transport, redactor), redactor


class FakeResponder:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses[request.url]


def test_static_header_uses_the_keychain_value_as_is_without_a_login_request(make_config):
    current, redactor = static_session(make_config, STATIC_LOGIN, base_url=None)
    assert current.login("Learner") == RoleToken("Learner", "Learner", ACCESS_CODE)
    assert current.headers("Learner") == {"X-Access-Code": ACCESS_CODE}
    assert current.auth("Learner") == ("X-Access-Code", "")
    assert current.transport.requests == [] and current.tokens() == (ACCESS_CODE,)
    assert redactor.text(f"code {ACCESS_CODE}") == "code [已脱敏]"


def test_static_header_verify_posts_the_template_and_needs_a_2xx(make_config):
    url = "https://site.example.test/api/login"
    current, _ = static_session(make_config, {**STATIC_LOGIN, "verify": VERIFY}, {url: HttpResponse(200, b"{}")})
    assert current.headers("Learner") == {"X-Access-Code": ACCESS_CODE}
    assert json.loads(current.transport.requests[0].body) == {"code": ACCESS_CODE, "who": "{account}"}
    rejected, _ = static_session(make_config, {**STATIC_LOGIN, "verify": VERIFY},
                                 {url: HttpResponse(401, ACCESS_CODE.encode())})
    with pytest.raises(LoginFailed) as caught:
        rejected.login("Learner")
    assert caught.value.reason == "凭证校验接口返回 401" and ACCESS_CODE not in str(caught.value)
    assert rejected.tokens() == ()
    no_target, _ = static_session(make_config, {**STATIC_LOGIN, "verify": VERIFY}, base_url=None)
    assert no_target.login_all(["Learner"]) == ({}, {"Learner": NO_TARGET})


def test_anonymous_needs_no_credentials_accounts_or_target(make_config):
    transport = FakeTransport({})
    current = Session(None, None, FakeCredentials(missing={ANONYMOUS_ROLE}), transport, make_redactor())
    assert current.login(ANONYMOUS_ROLE) == RoleToken(ANONYMOUS_ROLE, ANONYMOUS_ROLE, "")
    assert current.headers(ANONYMOUS_ROLE) == {} and current.auth(ANONYMOUS_ROLE) is None
    assert current.tokens() == () and transport.requests == []
    configured, _ = session({})
    assert configured.headers(ANONYMOUS_ROLE) == {} and configured.auth("Company") == ("Authorization", "Bearer ")


def test_probe_roles_prefer_the_request_then_accounts_then_anonymous(make_config):
    configured = make_config(accounts={"roles": {"Company": {"keychain": "k"}}, "login": {
        "endpoint": "/l", "bodyTemplate": {}, "tokenPath": "t"}})
    assert probe_roles(configured, ("anonymous",)) == ("anonymous",)
    assert probe_roles(configured) == ("Company",)
    assert probe_roles(make_config(accounts=None)) == (ANONYMOUS_ROLE,)


def test_keychain_account_is_the_role_for_a_static_header(make_config):
    class Keychain:
        def read_account(self, item):
            raise AssertionError("固定请求头凭证不读账号名")

    config = make_config(accounts={"roles": {"Learner": {"keychain": "k"}}, "login": STATIC_LOGIN})
    assert KeychainCredentials(config, Keychain()).account("Learner") == "Learner"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        status = 200 if json.loads(body)["ok"] else 500
        data = json.dumps({"echo": self.headers["X-Probe"]}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_urllib_transport_returns_status_and_body_for_errors_too(server):
    transport = UrllibTransport()
    good = transport(HttpRequest("POST", f"{server}/x", {"X-Probe": "a"}, b'{"ok": true}'))
    bad = transport(HttpRequest("POST", f"{server}/x", {"X-Probe": "b"}, b'{"ok": false}'))
    assert (good.status, json.loads(good.body)) == (200, {"echo": "a"}) and good.ok
    assert (bad.status, json.loads(bad.body)) == (500, {"echo": "b"}) and not bad.ok
    assert good.elapsed_ms >= 0


def test_urllib_transport_reports_connection_errors(server):
    response = UrllibTransport()(HttpRequest("GET", "http://127.0.0.1:9/none", timeout_seconds=2))
    assert response.status is None and response.error


def test_urllib_transport_uses_the_given_proxy_table(server):
    proxied = UrllibTransport(proxies={"http": server, "no": "localhost"})
    response = proxied(HttpRequest("POST", "http://far.example.invalid/x", {"X-Probe": "p"}, b'{"ok": true}'))
    assert (response.status, json.loads(response.body)) == (200, {"echo": "p"})  # 经代理(测试服务器)转发
    direct = UrllibTransport(proxies={"http": "http://127.0.0.1:9", "no": "127.0.0.1,localhost"})
    response = direct(HttpRequest("POST", f"{server}/x", {"X-Probe": "d"}, b'{"ok": true}'))
    assert response.status == 200
    assert UrllibTransport().opener(server) is None
