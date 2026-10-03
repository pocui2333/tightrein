"""给人读的文字的语言(project.language)：语言名与按语言取固定文字。

固定文字(Issue 小节标题、标签、评论框架)有 zh、en、ja 三套；其他语言代码取英文。语言代码可带地区(zh-CN)，
按主语言取值。
"""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT = "en"
NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語"}


def primary(code: str) -> str:
    return code.split("-", 1)[0].lower()


def name(code: str) -> str:
    """写进提示的语言名；不认识的代码原样返回。"""
    return NAMES.get(primary(code), code)


def pick(texts: Mapping[str, str], code: str) -> str:
    """按语言取一条固定文字；没有这种语言时取英文。"""
    return texts.get(primary(code)) or texts[DEFAULT]
