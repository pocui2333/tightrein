"""每次运行的 schemathesis.toml，只含非敏感值。

- `[headers]`：凭证请求头写成 `Authorization = "Bearer ${TIGHTREIN_TOKEN}"`(static-header 时为
  `<请求头> = "${TIGHTREIN_TOKEN}"`)，Schemathesis 读配置时从环境变量展开，凭证不落盘；匿名运行没有这一段；
- `workers`：并发数；
- `[output.sanitization]`：保持开启。Schemathesis 4.28.0 的 keys-to-sanitize 一旦给出就替换缺省清单而不是追加，
  所以写「缺省清单(取自 SanitizationConfig)+ 项目追加项 + 凭证请求头名」。
TOML 的字符串以 JSON 字符串写出(两者基本字符串的转义规则一致)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from schemathesis.config import SanitizationConfig

from tightrein.store.files.atomic import write_text

TOKEN_ENV = "TIGHTREIN_TOKEN"


def sanitize_keys(extra: Iterable[str]) -> list[str]:
    keys = list(SanitizationConfig().keys_to_sanitize)
    for key in extra:
        if key.lower() not in keys:
            keys.append(key.lower())
    return keys


def render(workers: int, extra_sanitize_keys: Iterable[str], auth: tuple[str, str] | None) -> str:
    """auth 为凭证请求头与值的前缀(`("Authorization", "Bearer ")`)；匿名运行为 None。"""
    extra = [*extra_sanitize_keys, *(() if auth is None else (auth[0],))]
    keys = ", ".join(_string(key) for key in sanitize_keys(extra))
    headers = "" if auth is None else (
        f"[headers]\n{_string(auth[0])} = {_string(auth[1] + '${' + TOKEN_ENV + '}')}\n\n")
    return (
        f"workers = {int(workers)}\n"
        "\n"
        f"{headers}"
        "[output.sanitization]\n"
        "enabled = true\n"
        f"keys-to-sanitize = [{keys}]\n"
    )


def write(path: Path, workers: int, extra_sanitize_keys: Iterable[str], auth: tuple[str, str] | None) -> Path:
    write_text(path, render(workers, extra_sanitize_keys, auth))
    return path


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
