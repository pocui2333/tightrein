"""取得接口凭证：凭证只在内存中持有，取得即登记进脱敏器，此后写出的任何内容中出现它都会被替换。

sites.json 的 api_fuzz.login 决定取法；没有 login 时以匿名身份运行(请求与复现命令都不带凭证)：
- token-endpoint(缺省)：POST <baseUrl><endpoint>，请求体取 bodyTemplate，其中字符串里的 `{account}`、`{password}`
  换成 secrets.json 的 api_fuzz.account 与 api_fuzz.password；响应须为 2xx 的 JSON，按 tokenPath(以 . 分隔的键)
  取出非空字符串，放进 `Authorization: Bearer <token>`；
- static-header：api_fuzz.password 就是凭证，原样放进 header 指定的请求头，不请求登录接口；配置了 verify 时先
  POST 该接口(bodyTemplate 中只替换 `{password}`)，2xx 即有效；
- 失败原因不含密码与响应体原文。一次运行内只登录一次由调用方保证(source 调一次，replay 按运行编号缓存)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tightrein.protocol.http import HttpRequest, HttpResponse, Transport
from tightrein.protocol.security import Redactor

TOKEN_ENDPOINT = "token-endpoint"
STATIC_HEADER = "static-header"
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
ACCOUNT_PLACEHOLDER = "{account}"
PASSWORD_PLACEHOLDER = "{password}"
ACCOUNT_SECRET = "api_fuzz.account"
PASSWORD_SECRET = "api_fuzz.password"
JSON_CONTENT_TYPE = "application/json"


class LoginFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"登录失败：{reason}")
        self.reason = reason


@dataclass(frozen=True)
class LoginSettings:
    kind: str
    endpoint: str | None  # token-endpoint 的登录接口，或 static-header 的校验接口(没有校验时为 None)
    body_template: Mapping[str, Any] | None
    token_path: str | None
    header: str = AUTHORIZATION_HEADER

    @property
    def auth(self) -> tuple[str, str]:
        """凭证所在的请求头与值的前缀。"""
        return (AUTHORIZATION_HEADER, BEARER_PREFIX) if self.kind == TOKEN_ENDPOINT else (self.header, "")

    @classmethod
    def from_sites(cls, group: Mapping[str, Any]) -> LoginSettings | None:
        """sites.json 的 api_fuzz 分组；没有 login 时为 None(匿名运行)。"""
        login = group.get("login")
        if login is None:
            return None
        if login.get("kind", TOKEN_ENDPOINT) == STATIC_HEADER:
            verify = login.get("verify") or {}
            return cls(STATIC_HEADER, verify.get("endpoint"), verify.get("bodyTemplate"), None, login["header"])
        return cls(TOKEN_ENDPOINT, login["endpoint"], login["bodyTemplate"], login["tokenPath"])


@dataclass(frozen=True)
class Credential:
    header: str
    prefix: str
    token: str = field(repr=False)

    @property
    def auth(self) -> tuple[str, str]:
        return self.header, self.prefix

    def headers(self) -> dict[str, str]:
        return {self.header: self.prefix + self.token}


def login(settings: LoginSettings | None, *, base_url: str, secrets: Mapping[str, str], transport: Transport,
          redactor: Redactor, timeout_s: float) -> Credential | None:
    """匿名运行返回 None。"""
    if settings is None:
        return None
    password = _secret(secrets, PASSWORD_SECRET)
    if settings.kind == STATIC_HEADER:
        if settings.endpoint is not None:
            _post(base_url, settings.endpoint, settings.body_template, ACCOUNT_PLACEHOLDER, password, transport,
                  timeout_s, "凭证校验接口")
        token = password
    else:
        account = _secret(secrets, ACCOUNT_SECRET)
        response = _post(base_url, settings.endpoint or "", settings.body_template, account, password, transport,
                         timeout_s, "登录接口")
        token = _token(response, settings.token_path or "")
    redactor.register(token)
    header, prefix = settings.auth
    return Credential(header, prefix, token)


def fill(template: Any, account: str, password: str) -> Any:
    if isinstance(template, str):
        return template.replace(ACCOUNT_PLACEHOLDER, account).replace(PASSWORD_PLACEHOLDER, password)
    if isinstance(template, Mapping):
        return {key: fill(value, account, password) for key, value in template.items()}
    if isinstance(template, list):
        return [fill(value, account, password) for value in template]
    return template


def extract(document: Any, path: str) -> Any:
    value = document
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _secret(secrets: Mapping[str, str], name: str) -> str:
    value = secrets.get(name)
    if not value:
        raise LoginFailed(f"secrets.json 中没有 {name}")
    return value


def _post(base_url: str, endpoint: str, template: Any, account: str, password: str, transport: Transport,
          timeout_s: float, label: str) -> HttpResponse:
    body = json.dumps(fill(template, account, password), ensure_ascii=False).encode("utf-8")
    response = transport(HttpRequest("POST", join_url(base_url, endpoint), timeout_s,
                                     {"Content-Type": JSON_CONTENT_TYPE, "Accept": JSON_CONTENT_TYPE}, body))
    if response.status is None:
        raise LoginFailed(f"{label}没有响应：{response.error}")
    if not response.ok:
        raise LoginFailed(f"{label}返回 {response.status}")
    return response


def _token(response: HttpResponse, token_path: str) -> str:
    try:
        document = json.loads(response.text())
    except ValueError as error:
        raise LoginFailed("登录接口的响应不是 JSON") from error
    token = extract(document, token_path)
    if not isinstance(token, str) or not token:
        raise LoginFailed(f"登录响应中 {token_path} 不是非空字符串")
    return token
