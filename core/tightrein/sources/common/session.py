"""按角色取得接口凭证(architecture/04 1.6)；凭证只在内存中持有。

accounts.login.kind 决定取凭证的方式(缺省 token-endpoint)：
- token-endpoint：账号名与密码由 Credentials 给出(KeychainCredentials 按 accounts.roles.<角色>.keychain 从钥匙串
  读取)；POST <base_url><accounts.login.endpoint>，请求体取 accounts.login.bodyTemplate，其中字符串里的
  `{account}`、`{password}` 替换为账号名与密码；响应须为 2xx 的 JSON，按 accounts.login.tokenPath(以 . 分隔的键)
  取出非空字符串作为 token，请求时放进 `Authorization: Bearer <token>`；
- static-header：钥匙串条目中的密码就是凭证，请求时原样放进 accounts.login.header 指定的请求头，不请求登录接口；
  账号名取角色名。配置了 accounts.login.verify 时先 POST 该接口(bodyTemplate 中只替换 `{password}`)，2xx 即有效；
- 匿名身份(保留角色名 anonymous)不需要凭证，请求不带凭证请求头；没有配置 accounts 时探针以匿名身份运行(probe_roles)；
- 取得的凭证立即登记到脱敏器，此后写出的任何内容中出现它都会被替换；失败原因不含密码与响应体原文；
- 同一角色在一次运行中只取一次凭证；login_all 返回成功的凭证与失败角色的原因；
- 非匿名角色在没有配置 accounts(LoginSettings 为空)时，或需要请求接口而没有目标地址时，以 LoginFailed 结束并写明原因。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from tightrein.config.project import ProjectConfig
from tightrein.config.secrets import Keychain, SecretError
from tightrein.sources.common.http import HttpRequest, HttpResponse, Transport
from tightrein.sources.common.redact import ProbeRedactor

ANONYMOUS_ROLE = "anonymous"
TOKEN_ENDPOINT = "token-endpoint"
STATIC_HEADER = "static-header"
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
BEARER_AUTH = (AUTHORIZATION_HEADER, BEARER_PREFIX)
ACCOUNT_PLACEHOLDER = "{account}"
PASSWORD_PLACEHOLDER = "{password}"
JSON_CONTENT_TYPE = "application/json"
NO_ACCOUNTS = "未配置 accounts，没有可登录的角色"
NO_TARGET = "没有目标地址(未配置 target.baseUrl，也没有给出 --target)"


class LoginFailed(Exception):
    def __init__(self, role: str, reason: str) -> None:
        self.role = role
        self.reason = reason
        super().__init__(f"角色 {role} 登录失败：{reason}")


class Credentials(Protocol):
    def account(self, role: str) -> str: ...

    def password(self, role: str) -> str: ...


class KeychainCredentials:
    def __init__(self, config: ProjectConfig, keychain: Keychain) -> None:
        self.config = config
        self.keychain = keychain

    def account(self, role: str) -> str:
        settings = LoginSettings.from_config(self.config)
        if settings is not None and settings.kind == STATIC_HEADER:
            return role
        return self.keychain.read_account(self.config.keychain_item(role))

    def password(self, role: str) -> str:
        return self.keychain.read(self.config.keychain_item(role)).value


def probe_roles(config: ProjectConfig, requested: tuple[str, ...] = ()) -> tuple[str, ...]:
    """探针运行的角色：显式给出的优先，其次 accounts.roles；都没有时以匿名身份运行。"""
    return requested or config.roles() or (ANONYMOUS_ROLE,)


@dataclass(frozen=True)
class LoginSettings:
    """endpoint 与 body_template 为 token-endpoint 的登录接口，或 static-header 的校验接口(没有校验时为空)。"""

    endpoint: str | None
    body_template: Mapping[str, Any] | None
    token_path: str | None
    kind: str = TOKEN_ENDPOINT
    header: str = AUTHORIZATION_HEADER

    @property
    def auth(self) -> tuple[str, str]:
        """凭证所在的请求头与值的前缀。"""
        return BEARER_AUTH if self.kind == TOKEN_ENDPOINT else (self.header, "")

    @classmethod
    def from_config(cls, config: ProjectConfig) -> LoginSettings | None:
        """没有配置 accounts 时为空。"""
        login = config.data.get("accounts", {}).get("login")
        if login is None:
            return None
        if login.get("kind", TOKEN_ENDPOINT) == STATIC_HEADER:
            verify = login.get("verify") or {}
            return cls(verify.get("endpoint"), verify.get("bodyTemplate"), None, STATIC_HEADER, login["header"])
        return cls(login["endpoint"], login["bodyTemplate"], login["tokenPath"])


@dataclass(frozen=True)
class RoleToken:
    role: str
    account: str
    token: str = field(repr=False)


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


class Session:
    def __init__(self, base_url: str | None, settings: LoginSettings | None, credentials: Credentials,
                 transport: Transport, redactor: ProbeRedactor, *, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url
        self.settings = settings
        self.credentials = credentials
        self.transport = transport
        self.redactor = redactor
        self.timeout_seconds = timeout_seconds
        self._tokens: dict[str, RoleToken] = {}

    def login(self, role: str) -> RoleToken:
        if role in self._tokens:
            return self._tokens[role]
        if role == ANONYMOUS_ROLE:
            result = RoleToken(role, role, "")
            self._tokens[role] = result
            return result
        if self.settings is None:
            raise LoginFailed(role, NO_ACCOUNTS)
        try:
            account = self.credentials.account(role)
            password = self.credentials.password(role)
        except SecretError as error:
            raise LoginFailed(role, str(error)) from error
        settings = self.settings
        if settings.kind == STATIC_HEADER:
            if settings.endpoint is not None:
                self._post(role, settings.endpoint, settings.body_template, ACCOUNT_PLACEHOLDER, password,
                           "凭证校验接口")
            token = password
        else:
            response = self._post(role, settings.endpoint or "", settings.body_template, account, password, "登录接口")
            token = self._token(role, response, settings.token_path or "")
        self.redactor.redactor.register(token)
        result = RoleToken(role, account, token)
        self._tokens[role] = result
        return result

    def _post(self, role: str, endpoint: str, template: Any, account: str, password: str, label: str) -> HttpResponse:
        if self.base_url is None:
            raise LoginFailed(role, NO_TARGET)
        body = json.dumps(fill(template, account, password), ensure_ascii=False).encode("utf-8")
        response = self.transport(HttpRequest(
            "POST", join_url(self.base_url, endpoint),
            {"Content-Type": JSON_CONTENT_TYPE, "Accept": JSON_CONTENT_TYPE}, body, self.timeout_seconds))
        if response.status is None:
            raise LoginFailed(role, f"{label}没有响应：{response.error}")
        if not response.ok:
            raise LoginFailed(role, f"{label}返回 {response.status}")
        return response

    @staticmethod
    def _token(role: str, response: HttpResponse, token_path: str) -> str:
        try:
            document = json.loads(response.text())
        except ValueError as error:
            raise LoginFailed(role, "登录接口的响应不是 JSON") from error
        token = extract(document, token_path)
        if not isinstance(token, str) or not token:
            raise LoginFailed(role, f"登录响应中 {token_path} 不是非空字符串")
        return token

    def auth(self, role: str) -> tuple[str, str] | None:
        """该角色的凭证所在的请求头与值的前缀；匿名身份或没有配置 accounts 时为空。"""
        if role == ANONYMOUS_ROLE or self.settings is None:
            return None
        return self.settings.auth

    def headers(self, role: str) -> dict[str, str]:
        """该角色请求需要带的凭证请求头(按需取凭证)；匿名身份为空。"""
        token = self.login(role)
        auth = self.auth(role)
        return {} if auth is None or not token.token else {auth[0]: auth[1] + token.token}

    def login_all(self, roles: Iterable[str]) -> tuple[dict[str, RoleToken], dict[str, str]]:
        tokens: dict[str, RoleToken] = {}
        failures: dict[str, str] = {}
        for role in roles:
            try:
                tokens[role] = self.login(role)
            except LoginFailed as error:
                failures[role] = error.reason
        return tokens, failures

    def tokens(self) -> tuple[str, ...]:
        return tuple(item.token for item in self._tokens.values() if item.token)
