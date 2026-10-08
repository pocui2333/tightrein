"""给项目探针脚本用的辅助函数。探针以 `{python}` 运行时可以直接 import：

    from tightrein.collect.project_probes import helpers

    found = helpers.read_input()
    ...
    helpers.emit([helpers.signal(...)], state={...})

- read_input / emit：读标准输入的输入 JSON；把输出 JSON 写到标准输出，写之前按 output.schema.json 校验，
  不合格抛 ValueError；
- signal：组装一条信号；
- secret：读登记过的凭据(登记在该探针的 secrets 中、由 tightrein 经环境变量传入的才读得到)，读到的值登记进脱敏器；
- redact：按 tightrein 的脱敏规则处理文本(含已读到的凭据)，证据与说明写出前调用。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from tightrein.protocol.handoff import load_schema, schema_errors
from tightrein.protocol.scripts import secret_env
from tightrein.protocol.security import Redactor

OUTPUT_SCHEMA = Path(__file__).with_name("output.schema.json")
_redactor = Redactor()


def read_input(stream: TextIO | None = None) -> dict[str, Any]:
    return json.loads((stream or sys.stdin).read())


def signal(location: str, symptom: str, evidence: Sequence[str], fingerprint: str, *,
           severity_hint: str | None = None, occurred_at: str | None = None,
           context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """evidence 至少一条；fingerprint 对同一个问题每次相同(检查项:对象，不带时间与数量)。"""
    item: dict[str, Any] = {"location": location, "symptom": redact(symptom),
                            "evidence": [redact(text) for text in evidence], "severityHint": severity_hint,
                            "fingerprint": fingerprint}
    if occurred_at is not None:
        item["occurredAt"] = occurred_at
    if context:
        item["context"] = dict(context)
    return item


def emit(signals: Sequence[Mapping[str, Any]], state: Mapping[str, Any] | None = None, notes: Sequence[str] = (),
         stream: TextIO | None = None) -> None:
    document = {"signals": [dict(item) for item in signals], "state": None if state is None else dict(state),
                "notes": [redact(note) for note in notes]}
    errors = schema_errors(document, load_schema(OUTPUT_SCHEMA))
    if errors:
        raise ValueError("输出不符合探针契约：" + "；".join(errors))
    target = stream or sys.stdout
    target.write(json.dumps(document, ensure_ascii=False))
    target.flush()


def secret(name: str) -> str:
    value = os.environ.get(secret_env(name))
    if value is None:
        raise PermissionError(f"凭据 {name} 没有在这个探针的 secrets 中登记，或 secrets.json 中没有这一项")
    _redactor.register(value)
    return value


def redact(text: str) -> str:
    return _redactor.text(text)
