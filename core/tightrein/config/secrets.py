"""从 macOS 钥匙串按条目名读取测试账号密码(architecture/01 5.4)。

条目用 `security add-generic-password -s <条目名> -a <账号> -w` 建立，读取时执行
`security find-generic-password -s <条目名> -w`，标准输出去掉末尾换行即为密码；账号名(登录接口的 `{account}`)
不是秘密值，由 `security find-generic-password -s <条目名>` 输出的属性中 `"acct"` 一行取得。读到的密码只在内存中使用：
不写文件、不写日志，异常信息只带条目名与 security 的错误输出；`Secret` 的 repr 与 str 不含密码，误打印也不会泄露。
每读到一个值即交给 register(通常为 `Redactor.register`)，此后事件与会话记录中出现该值时一律脱敏。
外部命令经 run 注入，测试不调用真实的 security；超时取 runtime.keychain.timeoutSeconds。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from tightrein.config import layers

SECURITY = "security"
ITEM_NOT_FOUND_EXIT_CODE = 44
ACCOUNT_ATTRIBUTE = re.compile(r'^\s*"acct"<blob>=(?:"(?P<text>.*)"|0x(?P<hex>[0-9A-Fa-f]+)\b.*)$', re.MULTILINE)

CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def run_command(args: Sequence[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False, timeout=timeout_seconds)


class SecretError(Exception):
    def __init__(self, item: str, reason: str) -> None:
        self.item = item
        self.reason = reason
        super().__init__(f"钥匙串条目 {item}：{reason}")


class SecretNotFound(SecretError):
    """钥匙串中没有该条目。"""


class SecretUnavailable(SecretError):
    """security 无法执行、执行失败或条目中的密码为空。"""


@dataclass(frozen=True)
class Secret:
    item: str
    value: str = field(repr=False)

    def __str__(self) -> str:
        return f"<钥匙串条目 {self.item}>"


class Keychain:
    """按条目名读取密码与账号名；同一条目在本实例中只读取一次。"""

    def __init__(self, register: Callable[[str], None], run: CommandRunner = run_command,
                 timeout_seconds: float | None = None) -> None:
        self._register = register
        self._run = run
        self._timeout = float(timeout_seconds if timeout_seconds is not None
                              else layers.core_value("runtime.keychain.timeoutSeconds"))
        self._cache: dict[str, Secret] = {}
        self._accounts: dict[str, str] = {}

    def _find(self, item: str, *args: str) -> str:
        if not item:
            raise ValueError("钥匙串条目名不能为空")
        try:
            result = self._run([SECURITY, "find-generic-password", "-s", item, *args], self._timeout)
        except (OSError, subprocess.SubprocessError) as error:
            raise SecretUnavailable(item, f"无法执行 {SECURITY}：{type(error).__name__}") from error
        if result.returncode == ITEM_NOT_FOUND_EXIT_CODE:
            raise SecretNotFound(item, "条目不存在")
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "没有错误输出"
            raise SecretUnavailable(item, f"{SECURITY} 退出码 {result.returncode}：{detail}")
        return result.stdout

    def read_account(self, item: str) -> str:
        """条目中记录的账号名；条目没有账号名时抛出 SecretUnavailable。"""
        if item in self._accounts:
            return self._accounts[item]
        match = ACCOUNT_ATTRIBUTE.search(self._find(item))
        if match is None:
            raise SecretUnavailable(item, "条目没有账号名(acct)，用 security add-generic-password -a <账号> 重新建立")
        text, hexadecimal = match.group("text"), match.group("hex")
        account = text if hexadecimal is None else bytes.fromhex(hexadecimal).decode("utf-8", errors="replace")
        if not account:
            raise SecretUnavailable(item, "条目中的账号名为空")
        self._accounts[item] = account
        return account

    def read(self, item: str) -> Secret:
        if item in self._cache:
            return self._cache[item]
        value = self._find(item, "-w").removesuffix("\n")
        if not value:
            raise SecretUnavailable(item, "条目中的密码为空")
        self._register(value)
        secret = Secret(item, value)
        self._cache[item] = secret
        return secret
