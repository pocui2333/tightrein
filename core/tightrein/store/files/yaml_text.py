"""YAML 的读写。读取时关闭时间戳的隐式转换：`2026-10-05` 保持为字符串，与 schema 中日期一律为字符串一致。
写出时字符串若会被其他 YAML 读取器解析成日期、数字或布尔，由 PyYAML 自动加引号。
"""

from __future__ import annotations

from typing import Any

import yaml

_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"


class StringDateLoader(yaml.SafeLoader):
    """安全加载器，去掉时间戳的隐式解析，其余规则与 SafeLoader 相同。"""


StringDateLoader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != _TIMESTAMP_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class YamlError(ValueError):
    """YAML 文本无法解析。"""


def load(text: str) -> Any:
    try:
        return yaml.load(text, Loader=StringDateLoader)
    except yaml.YAMLError as error:
        raise YamlError(f"YAML 无法解析：{error}") from error


def dump(data: Any) -> str:
    """按插入顺序写出，保留中文，块样式。"""
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
