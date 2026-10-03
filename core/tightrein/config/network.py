"""本机网络代理(本机用户配置的 network 段，architecture/01 5.3)：翻译成子进程环境与核心 HTTP 客户端的代理表。

- 配置了 network.proxy 时，http_proxy、https_proxy、HTTP_PROXY、HTTPS_PROXY 为该地址，no_proxy、NO_PROXY 为
  network.noProxy 加本机回环地址(本机服务与 local-run 总是直连)；没有配置时环境原样沿用当前进程的代理变量；
- 组装根只生成一次环境，git、gh、agent 工具、扩展、Schemathesis、Playwright、Semgrep 与本机服务都从组装根取得；
- 核心 HTTP 客户端按同一份环境得到代理表，直连判断只看其中的 no 项(proxies)。
代理地址带用户名或密码时按凭证处理：组装根把密码登记到脱敏器，agent 子进程的环境照旧去掉这类代理变量
(guards.credentials.build_env)。

换路(访问 GitHub 的 git 远程命令、gh 与第三方 skill 下载遇到网络类错误时，由 vcs.process 与 packaging.third_party
调用)：按 runtime.network.rerouteHost 判断当前路线——环境中有代理地址且该主机不被 no_proxy 绕过为经代理，否则为直连
(no_proxy 的项等于该主机或为其上级域名、或为 `*` 时绕过，与 curl、Go 的规则一致)；另一条路为经代理时在 no_proxy 追加
该主机，为直连时去掉绕过该主机及其子域名的项；环境中没有任何代理地址时没有另一条路。网络类错误按 runtime.network.errorPatterns
(正则，不区分大小写)匹配错误输出判断。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from tightrein.config.user import UserConfig

PROXY_NAMES = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
NO_PROXY_NAMES = ("no_proxy", "NO_PROXY")
LOOPBACK = ("localhost", "127.0.0.1", "::1")
PROXY_SUFFIX = "_proxy"
NO_PROXY_SCHEME = "no"
ROUTE_PROXY_NAMES = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")
DIRECT = "直连"
PROXIED = "经代理"
ANY_HOST = "*"


def environ(base: Mapping[str, str], user: UserConfig) -> dict[str, str]:
    """子进程使用的环境：base 加上本机用户配置的代理。"""
    found = dict(base)
    if user.network_proxy is None:
        return found
    bypass = ",".join(dict.fromkeys([*user.no_proxy, *LOOPBACK]))
    found.update({name: user.network_proxy for name in PROXY_NAMES})
    found.update({name: bypass for name in NO_PROXY_NAMES})
    return found


def proxies(environment: Mapping[str, str]) -> dict[str, str]:
    """与 urllib.request.getproxies_environment 相同的规则(小写变量优先)，但读给出的映射而不是进程环境。"""
    found: dict[str, str] = {}
    for lower in (False, True):
        for name, value in environment.items():
            if value and name.lower().endswith(PROXY_SUFFIX) and (name == name.lower()) == lower:
                found[name[:-len(PROXY_SUFFIX)].lower()] = value
    return found


def proxy_password(user: UserConfig) -> str | None:
    """代理地址中的密码；没有时为空。"""
    return None if user.network_proxy is None else urlsplit(user.network_proxy).password


def masked_proxy(proxy: str) -> str:
    """把代理地址中的密码换成 [已脱敏]，供 config show 显示。"""
    password = urlsplit(proxy).password
    return proxy if password is None else proxy.replace(f":{password}@", ":[已脱敏]@", 1)


@dataclass(frozen=True)
class Reroute:
    """一次换路：action 为已脱敏的命令或地址，before、after 为原路线与新路线，error 为原路线的错误。"""

    action: str
    before: str
    after: str
    error: str
    succeeded: bool

    def text(self) -> str:
        result = "成功" if self.succeeded else "仍失败"
        return f"{self.action}：{self.before}失败({self.error})，改为{self.after}重试{result}"

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "before": self.before, "after": self.after, "error": self.error,
                "succeeded": self.succeeded}


def _proxy_address(environment: Mapping[str, str]) -> str | None:
    return next((environment[name] for name in ROUTE_PROXY_NAMES if environment.get(name)), None)


def _entries(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bypasses(entry: str, host: str) -> bool:
    name = entry.lower().lstrip(".")
    return name == ANY_HOST or host == name or host.endswith("." + name)


def _covers(entry: str, host: str) -> bool:
    """项绕过该主机，或是该主机的子域名(gh 访问 api.github.com、下载访问 codeload.github.com)。"""
    return _bypasses(entry, host) or entry.lower().lstrip(".").endswith("." + host)


def route(environment: Mapping[str, str], host: str) -> str:
    """访问 host 的当前路线：DIRECT 或 PROXIED。"""
    if _proxy_address(environment) is None:
        return DIRECT
    bypass = [entry for name in NO_PROXY_NAMES for entry in _entries(environment.get(name, ""))]
    return DIRECT if any(_bypasses(entry, host) for entry in bypass) else PROXIED


def rerouted(environment: Mapping[str, str], host: str) -> dict[str, str] | None:
    """访问 host 走另一条路的环境；环境中没有代理地址(没有另一条路)时为 None。"""
    if _proxy_address(environment) is None:
        return None
    found = dict(environment)
    if route(environment, host) == PROXIED:
        for name in NO_PROXY_NAMES:
            found[name] = ",".join([*_entries(found.get(name, "")), host])
    else:
        for name in NO_PROXY_NAMES:
            if name in found:
                found[name] = ",".join(entry for entry in _entries(found[name]) if not _covers(entry, host))
    return found


def is_network_failure(text: str, patterns: Iterable[str]) -> bool:
    """错误输出是否属于网络类错误(超时、连接重置或拒绝、TLS 握手失败、无法解析主机等)。"""
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
