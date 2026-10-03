"""索引与查询共用的文本预处理(architecture/03 1.6.1)。

FTS5 的 unicode61 分词器把 Unicode 类别为字母(L*)、数字(N*)与私用区(Co)的字符视为词字符，连续的词字符组成一个词；
中日韩文字属于字母类，连续的中文会成为一个词，「权限」因此命中不了「数据权限校验」。写入索引前与查询前都经过
split_cjk：每个汉字、假名与韩文字符两侧加空格，各自成为一个词，查询时以短语要求这些字相邻且顺序一致。
其余字符保持原样，由分词器按分隔符处理；大小写与变音符号由分词器处理。
"""

from __future__ import annotations

import re
import unicodedata

CJK_RANGES = (
    "ᄀ-ᇿ"          # 韩文字母
    "぀-ヿ"          # 平假名、片假名
    "㄰-㆏"          # 韩文兼容字母
    "ㇰ-ㇿ"          # 片假名扩展
    "㐀-䶿"          # 汉字扩展 A
    "一-鿿"          # 汉字
    "가-힯"          # 韩文音节
    "豈-﫿"          # 兼容汉字
    "ｦ-ﾟ"          # 半角片假名
    "\U00020000-\U0003134f"  # 汉字扩展 B 到 G
)
CJK_CHAR = re.compile(f"[{CJK_RANGES}]")
WHITESPACE = re.compile(r"\s+")
LATIN_CHARS_PER_TOKEN = 4


def split_cjk(text: str) -> str:
    """每个中日韩字符两侧加空格，空白规整为单个空格。"""
    return WHITESPACE.sub(" ", CJK_CHAR.sub(lambda match: f" {match.group(0)} ", text)).strip()


def is_token_char(char: str) -> bool:
    """unicode61 视为词字符的字符。"""
    category = unicodedata.category(char)
    return category[0] in ("L", "N") or category == "Co"


def has_token(text: str) -> bool:
    return any(is_token_char(char) for char in text)


def estimate_tokens(text: str) -> int:
    """估算 token 数：中日韩字符每字计 1，其余非空白字符每 4 个计 1(向上取整)。只用于上限判断与报告。"""
    cjk = len(CJK_CHAR.findall(text))
    others = sum(1 for char in text if not char.isspace()) - cjk
    return cjk + -(-others // LATIN_CHARS_PER_TOKEN)
