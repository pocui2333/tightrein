import subprocess
import sys

import pytest

from tightrein.observability.redact import REDACTED, Redactor, looks_like_credential


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"

POSITIVE = [
    ("Authorization: Bearer abc.def-ghi", "Authorization: [已脱敏] [已脱敏]"),
    (f"token {JWT} 已过期", "token [已脱敏] 已过期"),
    ("key sk-ant-api03-abcdefghijklmnop", "key [已脱敏]"),
    ("推送用 ghp_abcdefghijklmnopqrstuvwxyz0123", "推送用 [已脱敏]"),
    ("aws AKIAABCDEFGHIJKLMNOP end", "aws [已脱敏] end"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "[已脱敏]"),
    ("password=hunter2&user=a", "password=[已脱敏]&user=a"),
    ('{"accessToken": "abc123", "id": 7}', '{"accessToken": "[已脱敏]", "id": 7}'),
    ("api_key: 'k-123'", "api_key: '[已脱敏]'"),
    ("Server=db;User Id=sa;Password=Secr3t;", "Server=db;User Id=sa;Password=[已脱敏];"),
    ("postgres://admin:s3cret@db:5432/app", "postgres://[已脱敏]@db:5432/app"),
    ("身份证 11010519491231002X 登记", "身份证 [已脱敏] 登记"),
    ("手机13812345678，备用 +86 13900001111", "手机[已脱敏]，备用 [已脱敏]"),
]

NEGATIVE = [
    "判为误报：上游已校验",
    "GET /api/Orders/42 返回 500",
    "R-20260929-021503-collect-api-fuzz",
    "trace 4bf92f3577b34da6a3ce929d0e0e4736",
    "耗时 1234567890 毫秒",
    "commit 3f9a1c2e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39",
    "https://staging.example.test/api/health",
    "token 已过期，需要重新登录",
    "订单号 202610050300123456789",
]


@pytest.fixture
def redactor():
    return Redactor()


@pytest.mark.parametrize("text, expected", POSITIVE)
def test_sensitive_text_is_redacted(redactor, text, expected):
    assert redactor.text(text) == expected


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_text_is_kept(redactor, text):
    assert redactor.text(text) == text


@pytest.mark.parametrize("text, _", POSITIVE)
def test_redaction_is_idempotent(redactor, text, _):
    once = redactor.text(text)
    assert redactor.text(once) == once


def test_registered_secrets_are_replaced_longest_first(redactor):
    redactor.register("abc")
    redactor.register("abcdef")
    redactor.register("")
    assert redactor.text("x abcdef y abc z") == f"x {REDACTED} y {REDACTED} z"


def test_registered_secrets_with_regex_characters(redactor):
    redactor.register("p@ss.w*rd(1)")
    assert redactor.text("登录失败，密码 p@ss.w*rd(1) 不正确") == f"登录失败，密码 {REDACTED} 不正确"


@pytest.mark.parametrize("key", [
    "password", "Password", "PASSWORD", "user_password", "accessToken", "refresh-token", "client_secret",
    "apiKey", "API_KEY", "x-api-key", "Authorization", "Cookie", "ConnectionString", "privateKey", "JWT",
])
def test_sensitive_keys(redactor, key):
    assert redactor.is_sensitive_key(key)


@pytest.mark.parametrize("key", ["input_tokens", "output_tokens", "keychain", "user", "cwd", "status", "key"])
def test_ordinary_keys(redactor, key):
    assert not redactor.is_sensitive_key(key)


def test_structures_are_redacted_recursively():
    redactor = Redactor(sensitive_keys=["phone", "id-card"])
    redactor.register("Pa55-w0rd!")
    data = {
        "query": "订单",
        "password": {"nested": "x"},
        "token": None,
        "phone": "any value",
        "idCard": "x",
        "items": ["login with Pa55-w0rd!", 3, ("postgres://a:b@h/db",)],
        "input_tokens": 1200,
    }
    assert redactor.value(data) == {
        "query": "订单",
        "password": REDACTED,
        "token": None,
        "phone": REDACTED,
        "idCard": REDACTED,
        "items": [f"login with {REDACTED}", 3, [f"postgres://{REDACTED}@h/db"]],
        "input_tokens": 1200,
    }
    assert data["password"] == {"nested": "x"}


@pytest.mark.parametrize("value, expected", [
    (JWT, True),
    ("Bearer abcdef", True),
    ("mysql://root:pw@localhost/db", True),
    ("password=x", True),
    ("/usr/local/bin:/usr/bin", False),
    ("Development", False),
    ("13812345678", False),
])
def test_looks_like_credential(value, expected):
    assert looks_like_credential(value) is expected


LONG = 200_000
TIME_LIMIT_SECONDS = 1.0
KILL_AFTER_SECONDS = 5
MEASURE = """
import sys
import time

from tightrein.observability.redact import Redactor

text = sys.stdin.read()
started = time.perf_counter()
Redactor().text(text)
print(time.perf_counter() - started)
"""


def redaction_seconds(text):
    """在子进程中计时：平方级的规则在这些输入上要跑很久，超过 KILL_AFTER_SECONDS 即终止，测试不会卡住。"""
    try:
        completed = subprocess.run([sys.executable, "-c", MEASURE], input=text, capture_output=True, text=True,
                                   timeout=KILL_AFTER_SECONDS, check=True)
    except subprocess.TimeoutExpired:
        return float(KILL_AFTER_SECONDS)
    return float(completed.stdout)


@pytest.mark.parametrize("text", [
    "a" * LONG,
    "a." * (LONG // 2),
    "token" * (LONG // 5),
    "QUJD+/" * (LONG // 6),
    "password=x " * (LONG // 11),
    "://a:b" * (LONG // 6),
], ids=["letters", "dotted", "keywords", "base64", "pairs", "urls"])
def test_long_text_is_redacted_in_linear_time(text):
    assert redaction_seconds(text) < TIME_LIMIT_SECONDS


@pytest.mark.parametrize("text, expected", [
    ("x" * 5000 + " password=hunter2", "x" * 5000 + " password=[已脱敏]"),
    ("id=password=hunter2", "id=password=[已脱敏]"),
    ('abc"client_secret": "s3"', 'abc"client_secret": "[已脱敏]"'),
    ("git+ssh://git:pw@example.test/repo", "git+ssh://[已脱敏]@example.test/repo"),
])
def test_prefixes_and_schemes_are_still_recognized(redactor, text, expected):
    assert redactor.text(text) == expected
