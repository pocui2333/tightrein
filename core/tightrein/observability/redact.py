"""脱敏规则(architecture/01 6.2，design 10.5)。

事件、会话记录与错误原文写盘前经过这里，token、密码、连接串、身份证号与手机号、登记的秘密值一律替换为 `[已脱敏]`：
- 登记的秘密值(钥匙串读出的密码等)按原文替换，较长的先替换，避免一个值是另一个值的一部分时留下残片；
- 文本按规则逐条替换：连接串中的用户名与密码、Bearer 与 Basic 凭证、JWT、常见服务的 token 格式、私钥块、
  键值形式的敏感项(`password=...`、`"token": "..."`，只替换值、保留键名便于追查)、身份证号、手机号；
- 每条规则的耗时与文本长度成线性：可变长的前缀只从一段字符的开头尝试(前面不是同类字符)，
  不在长串字母数字的每个位置上回溯；日志、会话记录中常见数十万字符的单行，平方级的规则会让写盘卡住；
- 映射中键名为敏感词(password、token、secret 等，大小写与分隔符不限)的值整体替换，另可登记额外的键名，
  例如 api-fuzz 的 `sanitizeKeys`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[已脱敏]"

SENSITIVE_WORDS = frozenset({
    "password", "passwd", "pwd", "passphrase", "secret", "token", "jwt", "authorization", "cookie", "credential",
    "credentials",
})
SENSITIVE_COMPOUNDS = ("apikey", "accesskey", "privatekey", "connectionstring")

_KEY_WORDS = (
    r"(?:password|passwd|pwd|passphrase|secret|token|jwt|authorization|cookie|credentials?"
    r"|api[_-]?key|access[_-]?key|private[_-]?key)"
)

CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"://[^\s/@:]+:[^\s/@]+@"), f"://{REDACTED}@"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]+=*"), rf"\1 {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), REDACTED),
)
KEY_VALUE_PATTERN = re.compile(
    rf"""(?i)(["']?(?<![\w.-])(?=[\w.-]*?{_KEY_WORDS})[\w.-]+["']?\s*[:=]\s*)(?!{re.escape(REDACTED)})"""
    r"""(?:"([^"]*)"|'([^']*)'|([^\s,;&"'}\]]+))"""
)
PERSONAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
        REDACTED,
    ),
    (re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), REDACTED),
)


def _key_value(match: re.Match[str]) -> str:
    prefix = match.group(1)
    if match.group(2) is not None:
        return f'{prefix}"{REDACTED}"'
    if match.group(3) is not None:
        return f"{prefix}'{REDACTED}'"
    return f"{prefix}{REDACTED}"


def _normalize(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _words(key: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", key)}


def looks_like_credential(value: str) -> bool:
    """值是否符合凭证格式；执行器清理子进程环境变量时用于黑名单兜底(architecture/02 3.7)。"""
    return any(pattern.search(value) for pattern, _ in CREDENTIAL_PATTERNS) or bool(KEY_VALUE_PATTERN.search(value))


class Redactor:
    """一个进程共用一个实例：钥匙串读出的值登记到这里，之后写出的全部内容都会替换掉它。"""

    def __init__(self, sensitive_keys: Iterable[str] = ()) -> None:
        self._extra_keys = frozenset(_normalize(key) for key in sensitive_keys)
        self._secrets: set[str] = set()
        self._secret_pattern: re.Pattern[str] | None = None

    def register(self, secret: str) -> None:
        if not secret or secret in self._secrets:
            return
        self._secrets.add(secret)
        ordered = sorted(self._secrets, key=lambda value: (-len(value), value))
        self._secret_pattern = re.compile("|".join(re.escape(value) for value in ordered))

    def is_sensitive_key(self, key: str) -> bool:
        normalized = _normalize(key)
        if normalized in self._extra_keys or _words(key) & SENSITIVE_WORDS:
            return True
        return any(compound in normalized for compound in SENSITIVE_COMPOUNDS)

    def text(self, value: str) -> str:
        if self._secret_pattern is not None:
            value = self._secret_pattern.sub(REDACTED, value)
        for pattern, replacement in CREDENTIAL_PATTERNS:
            value = pattern.sub(replacement, value)
        value = KEY_VALUE_PATTERN.sub(_key_value, value)
        for pattern, replacement in PERSONAL_PATTERNS:
            value = pattern.sub(replacement, value)
        return value

    def value(self, data: Any) -> Any:
        """递归处理映射、列表与字符串，返回新对象；其他标量原样返回。"""
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, Mapping):
            return {
                key: REDACTED if item is not None and isinstance(key, str) and self.is_sensitive_key(key)
                else self.value(item)
                for key, item in data.items()
            }
        if isinstance(data, (list, tuple)):
            return [self.value(item) for item in data]
        return data
