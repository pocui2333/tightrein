"""接口描述：按接入清单选的方法(spec_source/ 下的 openapi_file、openapi_url)取得，解析后统一写成 JSON 交给 Schemathesis。

- 方法的参数来自 controls."collect.api_fuzz".<方法名>，按方法清单(<方法>.yaml)校验(collect/common/methods)；
- 取自仓库的接口描述按 commit 缓存在 data/cache/openapi/<commit>.json：同一 commit 的文件内容不会变，不再重复读取；
- 方法报「不适用」(文件不存在)时 api_fuzz 跳过，不是失败；不来自仓库的接口描述(工作区文件、URL)不需要 worktree；
- 内容可以是 JSON 或 YAML，必须是含 paths 对象的映射，版本由 openapi(3.0.x、3.1.x)或 swagger(2.0)字段判断；
- 哈希取规范化后的 JSON(键排序)：接口描述没变、也没有新部署时整次跳过(source.py)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tightrein.collect.common.source import SourceInvalid
from tightrein.protocol import methods
from tightrein.protocol.git import Git
from tightrein.protocol.http import Transport
from tightrein.settings.load import Settings
from tightrein.store.files.atomic import write_text

SOURCE = "collect.api_fuzz"
PACKAGE = "tightrein.collect.api_fuzz.spec_source"
CACHE_DIR = "openapi"
SPEC_FILE = "openapi.json"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
SUPPORTED_VERSIONS = (re.compile(r"^3\.0(\.|$)"), re.compile(r"^3\.1(\.|$)"))
SWAGGER_VERSION = "2.0"


class SpecNotApplicable(Exception):
    """方法不适用(例如仓库里没有这个文件)：api_fuzz 本次跳过并写明原因。"""


@dataclass(frozen=True)
class SpecRequest:
    """方法取接口描述时可用的输入；不需要的方法不读。"""

    options: Mapping[str, Any]
    git: Git  # 项目主仓库(只读)：按 commit 读文件，不需要 worktree
    workspace: Path
    commit: str | None  # 被测部署的 commit；没有部署记录时为 None(读主分支)
    transport: Transport


@dataclass(frozen=True)
class SpecText:
    text: str
    label: str  # 写进说明与错误：文件路径或 URL


@dataclass(frozen=True)
class Spec:
    document: dict[str, Any]
    path: Path  # 交给 Schemathesis 的 JSON 文件
    hash: str
    label: str
    cached: bool


def ensure(*, method: str, settings: Settings, secrets: Mapping[str, str], request: SpecRequest,
           cache_dir: Path, raw_dir: Path) -> Spec:
    """取得接口描述；方法不适用时抛 SpecNotApplicable，内容不合格抛 SourceInvalid。"""
    loaded = methods.load(PACKAGE, method)
    configured = methods.configure(loaded, settings=settings, source=SOURCE, secrets=secrets)
    given = SpecRequest(configured.options, request.git, request.workspace, request.commit, request.transport)
    key = loaded.module.cache_key(given)
    cached = None if key is None else cache_dir / CACHE_DIR / f"{key}.json"
    if cached is not None and cached.is_file():
        document = json.loads(cached.read_text(encoding="utf-8"))
        return Spec(document, cached, digest(document), loaded.module.describe(given), True)
    fetched: SpecText = loaded.module.fetch(given)
    document = parse(fetched.text, fetched.label)
    target = cached or raw_dir / SPEC_FILE
    write_text(target, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return Spec(document, target, digest(document), fetched.label, False)


def parse(text: str, label: str) -> dict[str, Any]:
    try:
        document = json.loads(text)
    except ValueError:
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SourceInvalid(f"接口描述 {label} 不是 JSON 或 YAML：{error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise SourceInvalid(f"接口描述 {label} 中没有 paths 对象")
    version = str(document.get("openapi", ""))
    if not any(pattern.match(version) for pattern in SUPPORTED_VERSIONS) \
            and str(document.get("swagger", "")) != SWAGGER_VERSION:
        raise SourceInvalid(f"接口描述 {label} 的版本无法识别：openapi 为 {document.get('openapi')}，"
                            f"swagger 为 {document.get('swagger')}")
    return document


def digest(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def operations(document: Mapping[str, Any], exclude: str | None = None) -> list[tuple[str, str]]:
    """接口描述中的操作(方法大写, 路由模板)，扣除排除范围，按路由与方法排序。"""
    pattern = None if exclude is None else re.compile(exclude)
    found = []
    for route, item in document.get("paths", {}).items():
        if not isinstance(item, Mapping) or (pattern is not None and pattern.search(route)):
            continue
        found += [(method.upper(), route) for method in HTTP_METHODS if isinstance(item.get(method), Mapping)]
    return sorted(found, key=lambda pair: (pair[1], pair[0]))
