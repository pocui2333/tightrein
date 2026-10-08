"""Playwright trace 的凭证清除。

trace 是 zip 包：*.trace、*.network 为每行一个 JSON 的记录(请求与响应头、上下文参数，其中 storageState 含本地存储)，
resources/ 下是请求与响应体。运行结束后逐个改写原始输出目录中的 trace.zip，改写后每行仍是合法的 JSON，trace 查看器
照常能打开：
- 名为 Authorization、Cookie、Set-Cookie 的请求头与响应头，cookies、localStorage、sessionStorage 数组各项的 value
  替换为已脱敏；
- 其余字符串经 protocol.security 的脱敏规则(登记过的密码与 token、JWT、Bearer 凭证等)；
- 不是 JSON 的文本条目整体经脱敏规则，二进制条目保持原样；改写失败的 trace 直接删除并写进说明。
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tightrein.protocol.security import CREDENTIAL_KIND, REDACTED, Redactor

TRACE_NAME = "trace.zip"
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie"})
VALUE_LISTS = frozenset({"cookies", "localStorage", "sessionStorage"})
MASK = REDACTED.format(kind=CREDENTIAL_KIND)


def clean_all(directory: Path, redactor: Redactor) -> list[str]:
    """改写目录下全部 trace.zip，返回要写进说明的条目。"""
    notes: list[str] = []
    if not directory.is_dir():
        return notes
    for path in sorted(directory.rglob(TRACE_NAME)):
        try:
            _rewrite(path, redactor)
        except (zipfile.BadZipFile, OSError) as error:
            path.unlink(missing_ok=True)
            notes.append(f"trace {path.relative_to(directory).as_posix()} 清除凭证失败({type(error).__name__})，已删除")
    return notes


def clean_text(text: str, redactor: Redactor) -> str:
    """整体是 JSON 或每行是 JSON 时按结构清除；否则按文本规则脱敏。"""
    try:
        return _dump(_clean(json.loads(text), redactor))
    except ValueError:
        pass
    lines = text.split("\n")
    try:
        parsed = [json.loads(line) if line.strip() else None for line in lines]
    except ValueError:
        return redactor.text(text)
    return "\n".join("" if item is None else _dump(_clean(item, redactor)) for item in parsed)


def _clean(value: Any, redactor: Redactor, in_value_list: bool = False) -> Any:
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, list):
        return [_clean(item, redactor, in_value_list) for item in value]
    if not isinstance(value, Mapping):
        return value
    name = value.get("name")
    sensitive = in_value_list or (isinstance(name, str) and name.lower() in SENSITIVE_HEADERS)
    return {key: MASK if key == "value" and sensitive else _clean(item, redactor, key in VALUE_LISTS)
            for key, item in value.items()}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _rewrite(path: Path, redactor: Redactor) -> None:
    handle, temporary = tempfile.mkstemp(prefix=".trace-", suffix=".zip", dir=path.parent)
    os.close(handle)
    try:
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                data = source.read(info)
                try:
                    data = clean_text(data.decode("utf-8"), redactor).encode("utf-8")
                except UnicodeDecodeError:
                    pass
                target.writestr(info, data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
