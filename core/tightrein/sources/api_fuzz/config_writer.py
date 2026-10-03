"""每次运行的 schemathesis.toml(architecture/04 2.4)，只含非敏感值。

- headers：凭证请求头，token-endpoint 为 `Authorization = "Bearer ${TIGHTREIN_TOKEN}"`，static-header 为
  `<accounts.login.header> = "${TIGHTREIN_TOKEN}"`，Schemathesis 读取配置时从环境变量展开，凭证不落盘；
  匿名身份没有 headers 段，该请求头名同时加入 keys-to-sanitize；
- hooks：自定义检查所在的模块，模块只在设置了模型与角色的环境变量时注册越权检查；
- workers：sources.api-fuzz.workers(缺省值在 config/defaults.yaml)；
- [output.sanitization]：保持开启；Schemathesis 给出 keys-to-sanitize 时会替换而不是追加缺省清单，
  这里写入缺省清单加 sources.api-fuzz.sanitizeKeys。
TOML 中的字符串以 JSON 字符串写出(两者的基本字符串转义规则一致)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from schemathesis.config import SanitizationConfig

from tightrein.sources.common.session import BEARER_AUTH

HOOKS_MODULE = "tightrein.sources.api_fuzz.hooks"
TOKEN_ENV = "TIGHTREIN_TOKEN"


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def sanitize_keys(extra: Iterable[str]) -> list[str]:
    keys = list(SanitizationConfig().keys_to_sanitize)
    for key in extra:
        if key.lower() not in keys:
            keys.append(key.lower())
    return keys


def render(workers: int, extra_sanitize_keys: Iterable[str], auth: tuple[str, str] | None = BEARER_AUTH) -> str:
    """auth 为凭证请求头与值的前缀；匿名身份为空。"""
    extra = [*extra_sanitize_keys, *(() if auth is None else (auth[0],))]
    keys = ", ".join(_string(key) for key in sanitize_keys(extra))
    headers = "" if auth is None else (
        f"[headers]\n{_string(auth[0])} = {_string(auth[1] + '${' + TOKEN_ENV + '}')}\n\n")
    return (
        f"hooks = {_string(HOOKS_MODULE)}\n"
        f"workers = {int(workers)}\n"
        "\n"
        f"{headers}"
        "[output.sanitization]\n"
        "enabled = true\n"
        f"keys-to-sanitize = [{keys}]\n"
    )


def write(path: Path, workers: int, extra_sanitize_keys: Iterable[str],
          auth: tuple[str, str] | None = BEARER_AUTH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(workers, extra_sanitize_keys, auth), encoding="utf-8")
    return path
