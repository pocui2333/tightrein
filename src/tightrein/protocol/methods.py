"""平台方法：每个方法是一个程序加一个同名清单(`<方法>.yaml`)，接新平台就在对应目录下加一对文件，不改调用方。

清单只声明：名称、用途、适用条件、取地址的 sites 分组(site)、凭据条目名(secret)、参数(optionsSchema)与限制；
取值不写在清单里：地址与纯配置来自 sites.json 的 site 分组，参数来自 settings 中该来源控制键下以方法名为键的一节，
凭据来自 secrets.json。三者合并后按 optionsSchema 校验，一次列出全部问题。
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from tightrein.protocol.external import Misconfigured
from tightrein.protocol.handoff import schema_errors
from tightrein.settings.load import Settings

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Method:
    name: str
    module: ModuleType
    manifest: dict[str, Any]


@dataclass(frozen=True)
class Configured:
    method: Method
    options: dict[str, Any]
    token: str | None  # 只给程序放进请求头


@cache
def load(package: str, name: str) -> Method:
    """`package` 下名为 name 的方法；名字只许小写字母、数字与下划线(配置里的值不能拼出任意模块)。"""
    if not _NAME.match(name):
        raise Misconfigured(f"方法名不合规：{name!r}")
    try:
        module = importlib.import_module(f"{package}.{name}")
    except ModuleNotFoundError as error:
        raise Misconfigured(f"没有这个方法：{package.rsplit('.', 1)[-1]}/{name}") from error
    manifest = yaml.safe_load(Path(module.__file__ or "").with_suffix(".yaml").read_text(encoding="utf-8"))
    return Method(name, module, manifest)


def exists(package: str, name: str) -> bool:
    """`package` 下有没有名为 name 的方法(程序与同名清单都在)。"""
    if not _NAME.match(name):
        return False
    spec = importlib.util.find_spec(f"{package}.{name}")
    return spec is not None and spec.origin is not None and Path(spec.origin).with_suffix(".yaml").exists()


def configure(method: Method, *, settings: Settings, source: str, secrets: Mapping[str, str],
              extra: Mapping[str, Any] | None = None) -> Configured:
    """合并 sites、该来源控制键下的方法参数与 extra，按清单校验；缺凭据而清单要求时报错。"""
    site = method.manifest.get("site")
    options = {**(settings.sites.get(site, {}) if site else {}), **settings.section(source).get(method.name, {}),
               **(extra or {})}
    errors = schema_errors(options, method.manifest["optionsSchema"])
    if errors:
        raise Misconfigured(f"{source} 的 {method.name} 参数不合格：" + "；".join(errors))
    secret = method.manifest.get("secret")
    token = secrets.get(secret) if secret else None
    if secret and token is None and method.manifest.get("secretRequired", False):
        raise Misconfigured(f"{method.name} 需要凭据：在 secrets.json 中写条目 {secret}")
    return Configured(method, options, token)
