"""命令行的文案表：`zh.json`、`en.json` 两份，程序按语言取，不在代码里写死给人读的文字。

键写成 `<区块>.<名>`，只在第一个点处分开：`status.waiting`、`points.implement.code`(名本身可以带点)。
值用 str.format 填入 `{名}`；缺键、缺值都直接报错，不悄悄显示空白。加一种语言只加一份 `<语言>.json`。
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

DIRECTORY = Path(__file__).parent
LANGUAGES = ("zh", "en")


class TextMissing(KeyError):
    def __init__(self, language: str, key: str) -> None:
        super().__init__(f"文案表 {language}.json 中没有 {key}")
        self.language = language
        self.key = key

    def __str__(self) -> str:
        return str(self.args[0])


def text(language: str, key: str, **values: Any) -> str:
    template = _lookup(language, key)
    try:
        return template.format(**values)
    except KeyError as error:
        raise TextMissing(language, f"{key} 的值 {error.args[0]}") from error


def has(language: str, key: str) -> bool:
    section, _, name = key.partition(".")
    return name in _table(language).get(section, {})


@cache
def _table(language: str) -> dict[str, dict[str, str]]:
    if language not in LANGUAGES:
        raise ValueError(f"不支持的语言：{language}(只有 {', '.join(LANGUAGES)})")
    data: dict[str, dict[str, str]] = json.loads((DIRECTORY / f"{language}.json").read_text(encoding="utf-8"))
    return data


def _lookup(language: str, key: str) -> str:
    section, _, name = key.partition(".")
    found = _table(language).get(section, {}).get(name)
    if not isinstance(found, str):
        raise TextMissing(language, key)
    return found
