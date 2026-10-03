"""Claude Code 的本地插件(architecture/09 6.2)。

skills 构建为本地插件市场 tightrein-local 中的插件 tightrein，调用名带插件名前缀(/tightrein:loop)，
不与 Claude Code 自带的同名 skill 冲突。插件目录中的 skill 用复制而不是链接：Claude Code 安装插件时会把插件
复制到自己的缓存目录。版本为核心版本加全部 skill 目录哈希的前 8 位，内容变化时版本随之变化。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from pathlib import Path

from tightrein import __version__

MARKETPLACE = "tightrein-local"
PLUGIN = "tightrein"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
MANIFEST_DIR = ".claude-plugin"
DESCRIPTION = "tightrein 缺陷闭环的 skills：采集、聚合、分诊、Issue、修复、验证、发布、学习与编排"
VERSION_HASH_LENGTH = 8
PARTIAL_SUFFIX = ".partial"


def plugin_version(hashes: Mapping[str, str]) -> str:
    text = "".join(f"{name}\t{digest}\n" for name, digest in sorted(hashes.items()))
    return f"{__version__}+{hashlib.sha256(text.encode('utf-8')).hexdigest()[:VERSION_HASH_LENGTH]}"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(build_dir: Path, sources: Mapping[str, Path], version: str) -> None:
    """在同级的临时目录中构建插件市场，完成后替换 build_dir；skill 目录按内容复制(跟随链接)。"""
    partial = build_dir.with_name(build_dir.name + PARTIAL_SUFFIX)
    shutil.rmtree(partial, ignore_errors=True)
    _write_json(partial / MANIFEST_DIR / "marketplace.json", {
        "name": MARKETPLACE,
        "owner": {"name": PLUGIN},
        "plugins": [{"name": PLUGIN, "source": f"./{PLUGIN}", "description": DESCRIPTION, "version": version}],
    })
    plugin = partial / PLUGIN
    _write_json(plugin / MANIFEST_DIR / "plugin.json", {"name": PLUGIN, "version": version, "description": DESCRIPTION})
    for name, source in sorted(sources.items()):
        shutil.copytree(source, plugin / "skills" / name)
    shutil.rmtree(build_dir, ignore_errors=True)
    partial.rename(build_dir)


def install_commands(claude: str, build_dir: Path, first: bool) -> list[list[str]]:
    if first:
        return [[claude, "plugin", "marketplace", "add", str(build_dir)], [claude, "plugin", "install", PLUGIN_ID]]
    return [[claude, "plugin", "marketplace", "update", MARKETPLACE], [claude, "plugin", "update", PLUGIN_ID]]


def uninstall_commands(claude: str) -> list[list[str]]:
    return [[claude, "plugin", "uninstall", PLUGIN_ID], [claude, "plugin", "marketplace", "remove", MARKETPLACE]]


def list_command(claude: str) -> list[str]:
    return [claude, "plugin", "list", "--json"]


def listed_version(output: str) -> str | None:
    """`claude plugin list --json` 的输出(插件对象的数组，含 id、version)中本插件的版本；没有安装或输出不是
    JSON 数组时为空。"""
    try:
        plugins = json.loads(output)
    except ValueError:
        return None
    if not isinstance(plugins, list):
        return None
    versions = [item.get("version") for item in plugins if isinstance(item, dict) and item.get("id") == PLUGIN_ID]
    return next((str(version) for version in versions if version), None)
