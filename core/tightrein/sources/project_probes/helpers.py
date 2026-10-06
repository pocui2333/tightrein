"""供项目探针复用的辅助函数(docs/how-to/write-project-probe.md)。探针以 `{python}` 运行时可以直接 import：

    from tightrein.sources.project_probes import helpers

    found = helpers.read_input()
    ...
    helpers.emit([helpers.signal(...)], state={...})

- read_input / emit：读标准输入的输入 JSON、把输出 JSON 写到标准输出(写之前按探针契约校验，不合格时抛出 ValueError)；
- signal：组装一条信号；
- secret：从钥匙串读取登记过的只读凭证(sources.project-probes[].keychain 之外的条目一律拒绝)，读到的值登记到脱敏器；
- redact：按核心的脱敏规则处理文本(含已读到的凭证)，证据与说明写出前调用；
- query_logs：经工作区配置的日志平台方法(extensions.log-platform)取日志，配置了 log-parse 时返回解析后的条目，
  否则返回原文行；实际调用 `tightrein project probe logs`，非 Python 的探针也可以直接调用这条命令。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from tightrein.config.secrets import Keychain, SecretError
from tightrein.contracts import validate
from tightrein.observability.redact import Redactor
from tightrein.sources.project_probes.runner import ENV_KEYCHAIN, ENV_WORKSPACE, OUTPUT_SCHEMA

CLI = "from tightrein.cli.main import entry; entry()"
_redactor = Redactor()


def read_input(stream: TextIO | None = None) -> dict[str, Any]:
    return json.loads((stream or sys.stdin).read())


def signal(location: str, symptom: str, evidence: Sequence[str], fingerprint: str, *,
           severity_hint: str | None = None, occurred_at: str | None = None,
           context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """一条信号；evidence 至少一条，fingerprint 对同一个问题每次相同。"""
    item: dict[str, Any] = {"location": location, "symptom": redact(symptom),
                            "evidence": [redact(text) for text in evidence], "severityHint": severity_hint,
                            "fingerprint": fingerprint}
    if occurred_at is not None:
        item["occurredAt"] = occurred_at
    if context:
        item["context"] = dict(context)
    return item


def emit(signals: Sequence[Mapping[str, Any]], state: Mapping[str, Any] | None = None,
         notes: Sequence[str] = (), stream: TextIO | None = None) -> None:
    document = {"signals": [dict(item) for item in signals], "state": None if state is None else dict(state),
                "notes": [redact(note) for note in notes]}
    errors = validate.validate(OUTPUT_SCHEMA, document)
    if errors:
        raise ValueError("输出不符合探针契约：" + "；".join(str(error) for error in errors))
    target = stream or sys.stdout
    target.write(json.dumps(document, ensure_ascii=False))
    target.flush()


def secret(item: str) -> str:
    allowed = [name for name in os.environ.get(ENV_KEYCHAIN, "").split(",") if name]
    if item not in allowed:
        raise PermissionError(f"钥匙串条目 {item} 没有在 sources.project-probes[].keychain 中登记")
    try:
        return Keychain(_redactor.register).read(item).value
    except SecretError as error:
        raise PermissionError(str(error)) from error


def redact(text: str) -> str:
    return _redactor.text(text)


def query_logs(query: str, since: str, until: str, limit: int = 1000, *, parse: bool = True) -> list[Any]:
    """since、until 为带时区的 ISO 时间(通常取输入的 window)。"""
    argv = [sys.executable, "-c", CLI, "probe", "logs", "--query", query, "--since", since, "--until", until,
            "--limit", str(limit), "--json"]
    if not parse:
        argv.append("--raw")
    workspace = os.environ.get(ENV_WORKSPACE)
    if workspace:
        argv += ["--workspace", workspace]
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    try:
        document = json.loads(completed.stdout)
    except ValueError as error:
        raise RuntimeError(f"tightrein project probe logs 没有输出 JSON(退出码 {completed.returncode})") from error
    if completed.returncode != 0:
        raise RuntimeError(f"tightrein project probe logs 失败：{document.get('errors') or document.get('result')}")
    return list(document["result"]["entries"])
