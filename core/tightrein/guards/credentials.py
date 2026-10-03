"""agent 进程拿不到凭证(architecture/02 3.4、3.7，design 9.5)。

- build_env：白名单只保留 PATH、HOME、USER、LOGNAME、SHELL、TMPDIR、TERM、LANG、LC_*、标准代理变量与适配器声明的非凭证变量；
  需要经代理访问模型服务的网络中，去掉代理变量会让 agent 工具的请求被拒；代理地址带用户名或密码时视为凭证去掉；
  保留下来的变量名称含 TOKEN、SECRET、PASSWORD、PASSWD、KEY、CREDENTIAL、AUTH、COOKIE、SESSION，或值符合凭证格式的
  一律去掉；追加 GIT_TERMINAL_PROMPT=0，只读任务追加 GIT_OPTIONAL_LOCKS=0。报告只记录被去掉的疑似凭证变量的名称，不记录值。
- worktree 中未被 git 跟踪(含被忽略的)且匹配 credentialFiles 的文件，与仓库本地配置中带用户名密码或 token 的远程地址、
  凭证助手，都是 credential-present。用户全局与系统配置不检查(macOS 的系统配置默认带 osxkeychain 凭证助手)。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tightrein.domain.enums import ViolationKind
from tightrein.guards.protected import matching_pattern
from tightrein.guards.report import Violation
from tightrein.observability.redact import looks_like_credential
from tightrein.vcs.git_read import GitReader

ALLOWED_NAMES = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "LANG"})
ALLOWED_PREFIXES = ("LC_",)
PROXY_NAMES = frozenset({"http_proxy", "https_proxy", "all_proxy", "no_proxy",
                         "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})
SENSITIVE_NAME_PARTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "KEY", "CREDENTIAL", "AUTH", "COOKIE", "SESSION")
FIXED = {"GIT_TERMINAL_PROMPT": "0"}
READONLY_FIXED = {"GIT_OPTIONAL_LOCKS": "0"}
CONFIG_PATTERN = r"^(remote\..*\.(url|pushurl)|credential\..*)$"


@dataclass(frozen=True)
class AgentEnvironment:
    env: dict[str, str]
    removed_names: tuple[str, ...]


def sensitive_name(name: str) -> bool:
    upper = name.upper()
    return any(part in upper for part in SENSITIVE_NAME_PARTS)


def proxy_with_credentials(name: str, value: str) -> bool:
    """代理地址中带用户名或密码。"""
    return name in PROXY_NAMES and "://" in value and urlsplit(value).username is not None


def build_env(base: Mapping[str, str], *, extra_names: Iterable[str] = (), readonly: bool = False) -> AgentEnvironment:
    allowed = ALLOWED_NAMES | PROXY_NAMES | set(extra_names)
    env: dict[str, str] = {}
    removed: set[str] = set()
    for name, value in base.items():
        suspicious = sensitive_name(name) or looks_like_credential(value) or proxy_with_credentials(name, value)
        if suspicious:
            removed.add(name)
        elif name in allowed or name.startswith(ALLOWED_PREFIXES):
            env[name] = value
    env.update(FIXED)
    if readonly:
        env.update(READONLY_FIXED)
    return AgentEnvironment(env, tuple(sorted(removed)))


def credential_files(git: GitReader, workdir: Path, patterns: Iterable[str]) -> list[Violation]:
    patterns = tuple(patterns)
    violations = []
    for path in git.untracked(workdir, include_ignored=True):
        pattern = matching_pattern(path, patterns)
        if pattern is not None:
            violations.append(Violation(ViolationKind.CREDENTIAL_PRESENT, path,
                                        f"worktree 中有未跟踪的凭证文件(匹配 {pattern})，删除或移出 worktree 后重试"))
    return violations


def _url_has_credentials(url: str) -> bool:
    if looks_like_credential(url):
        return True
    if "://" not in url:
        return False
    parts = urlsplit(url)
    return parts.password is not None or (parts.username is not None and looks_like_credential(parts.username))


def config_credentials(git: GitReader, workdir: Path) -> list[Violation]:
    violations = []
    for key, values in sorted(git.local_config(workdir, CONFIG_PATTERN).items()):
        if key.startswith("credential."):
            violations.append(Violation(ViolationKind.CREDENTIAL_PRESENT, None,
                                        f"仓库本地配置了凭证助手 {key}，用 git config --local --unset-all {key} 删除"))
        elif any(_url_has_credentials(value) for value in values):
            violations.append(Violation(ViolationKind.CREDENTIAL_PRESENT, None,
                                        f"{key} 中含有用户名密码或 token，改为不带凭证的地址"))
    return violations
