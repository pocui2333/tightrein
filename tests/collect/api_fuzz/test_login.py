import json

import pytest

from tightrein.collect.api_fuzz import login
from tightrein.collect.api_fuzz.login import Credential, LoginFailed, LoginSettings
from tightrein.protocol.http import HttpResponse
from tightrein.protocol.security import Redactor

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiQ29tcGFueSJ9.c2lnbmF0dXJl"
SITES = {"login": {"endpoint": "/api/Auth/Login", "tokenPath": "data.token",
                   "bodyTemplate": {"userName": "{account}", "password": "{password}", "remember": True}}}
SECRETS = {"api_fuzz.account": "test-company", "api_fuzz.password": "pw-1"}


class Transport:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.response


def ok(token=TOKEN):
    return HttpResponse(200, json.dumps({"data": {"token": token}}).encode())


def test_template_filling_and_token_path():
    assert login.fill({"a": "{account}", "b": ["{password}", 1], "c": "x-{account}"}, "u", "p") == {
        "a": "u", "b": ["p", 1], "c": "x-u"}
    assert login.extract({"data": {"token": "t"}}, "data.token") == "t"
    assert login.extract({"data": []}, "data.token") is None


def test_login_posts_the_filled_template_and_registers_the_token():
    transport = Transport(ok())
    redactor = Redactor()
    credential = login.login(LoginSettings.from_sites(SITES), base_url="https://staging.example.test/",
                             secrets=SECRETS, transport=transport, redactor=redactor, timeout_s=30)
    assert credential == Credential("Authorization", "Bearer ", TOKEN)
    assert credential.headers() == {"Authorization": f"Bearer {TOKEN}"} and TOKEN not in repr(credential)
    request = transport.requests[0]
    assert (request.method, request.url) == ("POST", "https://staging.example.test/api/Auth/Login")
    assert json.loads(request.body) == {"userName": "test-company", "password": "pw-1", "remember": True}
    assert TOKEN not in redactor.text(f"token {TOKEN}")


@pytest.mark.parametrize(("response", "reason"), [
    (HttpResponse(None, error="URLError: refused"), "登录接口没有响应：URLError: refused"),
    (HttpResponse(401, b'{"password": "pw-1"}'), "登录接口返回 401"),
    (HttpResponse(200, b"<html>"), "登录接口的响应不是 JSON"),
    (HttpResponse(200, b'{"data": {"token": ""}}'), "登录响应中 data.token 不是非空字符串"),
])
def test_login_failures_never_carry_the_password_or_the_body(response, reason):
    with pytest.raises(LoginFailed) as caught:
        login.login(LoginSettings.from_sites(SITES), base_url="https://h", secrets=SECRETS,
                    transport=Transport(response), redactor=Redactor(), timeout_s=30)
    assert caught.value.reason == reason and "pw-1" not in str(caught.value)


def test_missing_secrets_fail_and_no_login_runs_anonymously():
    with pytest.raises(LoginFailed, match="api_fuzz.password"):
        login.login(LoginSettings.from_sites(SITES), base_url="https://h", secrets={}, transport=Transport(ok()),
                    redactor=Redactor(), timeout_s=30)
    assert login.login(LoginSettings.from_sites({}), base_url="https://h", secrets={}, transport=Transport(ok()),
                       redactor=Redactor(), timeout_s=30) is None


def test_static_header_uses_the_secret_as_is_without_a_login_request():
    transport = Transport(ok())
    redactor = Redactor()
    settings = LoginSettings.from_sites({"login": {"kind": "static-header", "header": "X-Access-Code"}})
    credential = login.login(settings, base_url="https://h", secrets={"api_fuzz.password": "code-7f3a9c41"},
                             transport=transport, redactor=redactor, timeout_s=30)
    assert credential.headers() == {"X-Access-Code": "code-7f3a9c41"} and transport.requests == []
    assert "code-7f3a9c41" not in redactor.text("code-7f3a9c41")
    verified = LoginSettings.from_sites({"login": {"kind": "static-header", "header": "X-Access-Code",
                                                   "verify": {"endpoint": "/api/check",
                                                              "bodyTemplate": {"code": "{password}"}}}})
    login.login(verified, base_url="https://h", secrets={"api_fuzz.password": "code-7f3a9c41"},
                transport=transport, redactor=redactor, timeout_s=30)
    assert json.loads(transport.requests[0].body) == {"code": "code-7f3a9c41"}
