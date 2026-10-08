"""运行项目自己写的只读脚本(项目探针、访问日志的项目数据源)：经标准输入交给它一个 JSON，读回标准输出。

- 命令相对工作区执行；`{python}` 换成 tightrein 自己的解释器；接入清单只写了脚本路径时，`.py` 用该解释器运行，
  其他直接执行(脚本要有可执行权限)；
- 环境变量只给白名单(security.child_env)，另加工作区路径、脚本名与登记过的凭据：凭据条目 `a.b-c` 以
  `TIGHTREIN_SECRET_A_B_C` 传入，只传登记的几项，值已在读取时登记进脱敏器；
- 退出码非 0、超时、无法启动都抛 SourceError，标准错误脱敏后写进原始输出 `<名>.stderr.log`。
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.protocol.external import Misconfigured, Unavailable
from tightrein.protocol.naming import segment
from tightrein.protocol.process import Command, ProcessRunner
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor, child_env

PYTHON_PLACEHOLDER = "{python}"
ENV_WORKSPACE = "TIGHTREIN_WORKSPACE"
ENV_NAME = "TIGHTREIN_SCRIPT_NAME"
SECRET_PREFIX = "TIGHTREIN_SECRET_"
STDERR_SUFFIX = ".stderr.log"
STDERR_TAIL_LINES = 3
_NOT_WORD = re.compile(r"[^A-Za-z0-9]+")


def run(*, name: str, command: Sequence[str], document: Mapping[str, Any], workspace: Path, runner: ProcessRunner,
        environ: Mapping[str, str], secrets: Mapping[str, str], secret_names: Sequence[str], redactor: Redactor,
        raw: RawDir, timeout_s: float) -> str:
    """返回标准输出；失败抛 Unavailable(原因不含凭据)。"""
    missing = [item for item in secret_names if item not in secrets]
    if missing:
        raise Misconfigured(f"{name} 登记的凭据在 secrets.json 中没有：{', '.join(missing)}")
    values = {ENV_WORKSPACE: str(workspace.absolute()), ENV_NAME: name,
              **{secret_env(item): secrets[item] for item in secret_names}}
    argv = tuple(sys.executable if part == PYTHON_PLACEHOLDER else part for part in command)
    outcome = runner.run(Command(argv, workspace, child_env(environ, set_values=values),
                                 stdin=json.dumps(document, ensure_ascii=False), timeout_s=timeout_s))
    stderr = redactor.text(outcome.stderr_tail)
    if stderr.strip():
        raw.write_text(f"{segment(name)}{STDERR_SUFFIX}", stderr)
    if outcome.start_error is not None:
        raise Unavailable(f"{name} 无法启动：{outcome.start_error}")
    if outcome.stopped_by is not None:
        raise Unavailable(f"{name} 被终止({outcome.stopped_by})：超过 {timeout_s:g} 秒或输出过大")
    if outcome.exit_code != 0:
        tail = " / ".join(stderr.strip().splitlines()[-STDERR_TAIL_LINES:]) or "没有错误输出"
        raise Unavailable(f"{name} 退出码 {outcome.exit_code}：{tail}")
    return outcome.stdout


def command_for(script: str) -> tuple[str, ...]:
    """接入清单中 custom 模块的脚本路径 → 命令。"""
    if not script:
        raise Misconfigured("接入清单中 custom 的模块没有写 script")
    return (PYTHON_PLACEHOLDER, script) if script.endswith(".py") else (script,)


def secret_env(name: str) -> str:
    return SECRET_PREFIX + _NOT_WORD.sub("_", name).strip("_").upper()
