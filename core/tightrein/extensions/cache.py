"""与 commit 绑定的扩展输出缓存(architecture/10 2.7)：data/specs/<commit>/<扩展点>.json 与 <扩展点>.meta.json。

- 缓存键：实现层、选用的方法编号、配置或清单中的命令、生效的 options 的哈希、技术栈的 version、项目扩展与核心方法
  命令文件内容的哈希；读取时键不一致、文件缺失或无法解析、输出不再符合该扩展点的 schema，都视为未命中，
  由调用方重新调用扩展。
- 只缓存成功的输出；核心默认、失败与 not-applicable 不写缓存。写入先写输出、后写元数据，中途失败只会未命中。
- 只用于 spec-export、authz-endpoints、authz-roles、page-routes；log-source、log-parse、static-tools、local-run 不缓存。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import ExtensionLayer
from tightrein.extensions import points
from tightrein.extensions.resolve import Implementation
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout


def options_hash(options: Mapping[str, Any]) -> str:
    text = json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cache_key(implementation: Implementation) -> dict[str, Any]:
    """写入 meta.json 并在读取时比对的缓存键。"""
    hashed = implementation.layer in (ExtensionLayer.PROJECT, ExtensionLayer.CORE)
    command_file = implementation.command_file if hashed else None
    return {
        "implementation": implementation.layer.value,
        "method": implementation.method,
        "command": list(implementation.command),
        "optionsHash": options_hash(implementation.options),
        "stackVersion": implementation.version if implementation.layer is ExtensionLayer.STACK else None,
        "commandFileHash": None if command_file is None else file_hash(command_file),
    }


class ExtensionCache:
    def __init__(self, layout: WorkspaceLayout, clock: Clock) -> None:
        self.layout = layout
        self.clock = clock

    def read(self, commit: str, implementation: Implementation) -> dict[str, Any] | None:
        """缓存命中时返回输出，否则为空。"""
        point = implementation.point
        try:
            meta = json.loads(self.layout.extension_meta(commit, point).read_text(encoding="utf-8"))
            output = json.loads(self.layout.extension_output(commit, point).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict) or not isinstance(output, dict):
            return None
        expected = cache_key(implementation)
        if {name: meta.get(name) for name in expected} != expected:
            return None
        if validate.validate(points.SPECS[point].output_schema, output):
            return None
        return output

    def write(self, commit: str, implementation: Implementation, output: Mapping[str, Any]) -> None:
        point = implementation.point
        text = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
        atomic.write_text(self.layout.extension_output(commit, point), text)
        meta = {"point": point.value, **cache_key(implementation),
                "generatedAt": format_iso(self.clock.now())}
        meta_text = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
        atomic.write_text(self.layout.extension_meta(commit, point), meta_text)
