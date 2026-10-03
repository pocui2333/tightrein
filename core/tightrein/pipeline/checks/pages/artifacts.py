"""trace 的凭证清除(architecture/04 1.6、3.6)。

Playwright 的 trace 是 zip 包：*.trace、*.network 为每行一个 JSON 的记录(请求与响应头、上下文参数，其中的
storageState 含本地存储)，resources/ 下保存请求与响应体。运行结束后逐个改写原始输出目录中的 trace.zip，
改写后每一行仍是合法的 JSON，trace 查看器照常可以打开：
- 名为 Authorization、Cookie、Set-Cookie 的请求头与响应头、cookies 数组各项、localStorage 数组各项的 value
  替换为 [已脱敏]；
- 其余字符串经脱敏规则处理(JWT、Bearer 凭证、登记过的密码与 token 等)；
- 不是 JSON 的文本条目整体经脱敏规则处理，二进制条目(截图、源码快照以外的非文本)保持原样。
无法改写的 trace 直接删除，在 notes 中注明。
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tightrein.observability.redact import REDACTED
from tightrein.sources.common.redact import ProbeRedactor

TRACE_NAME = "trace.zip"
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie"})
VALUE_LISTS = frozenset({"cookies", "localStorage", "sessionStorage"})


def _clean(value: Any, redactor: ProbeRedactor, in_value_list: bool = False) -> Any:
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, list):
        return [_clean(item, redactor, in_value_list) for item in value]
    if not isinstance(value, Mapping):
        return value
    name = value.get("name")
    sensitive = in_value_list or (isinstance(name, str) and name.lower() in SENSITIVE_HEADERS)
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key == "value" and sensitive:
            cleaned[key] = REDACTED
        else:
            cleaned[key] = _clean(item, redactor, key in VALUE_LISTS)
    return cleaned


def clean_text(text: str, redactor: ProbeRedactor) -> str:
    """整体是 JSON 或每行是 JSON 时按结构清除；否则按文本规则脱敏。"""
    try:
        return json.dumps(_clean(json.loads(text), redactor), ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        pass
    lines = text.split("\n")
    try:
        parsed = [json.loads(line) if line.strip() else None for line in lines]
    except ValueError:
        return redactor.text(text)
    return "\n".join("" if item is None else json.dumps(_clean(item, redactor), ensure_ascii=False,
                                                        separators=(",", ":")) for item in parsed)


def _rewrite(path: Path, redactor: ProbeRedactor) -> None:
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


def clean_traces(directory: Path, redactor: ProbeRedactor) -> list[str]:
    """改写目录下全部 trace.zip，返回需要写入运行摘要的说明。"""
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
