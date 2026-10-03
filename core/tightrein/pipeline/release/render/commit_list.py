"""提交确认的说明(architecture/07 19.1 第 4 步)：提交信息、按文件名排序的文件清单与照常提交时接受的未通过项。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def render(message: str, files: Sequence[str], accepted: Sequence[Mapping[str, Any]] = ()) -> str:
    lines = ["提交信息：", message, "", "文件清单(按文件名排序)：", *(f"- {path}" for path in files)]
    if accepted:
        lines += ["", "照常提交，接受的未通过项：", *(f"- {item['check']}：{item['problem']}" for item in accepted)]
    return "\n".join(lines) + "\n"
