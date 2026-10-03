"""工作区 normalize.yaml 的读取与校验(design 2.5)：项目特有的规范化规则，在内置默认规则之后按顺序执行。

格式为 `rules: [{pattern, replacement}]`，pattern 是 Python 正则。文件不存在时没有项目规则；不合格时一次列出全部问题。
"""

from __future__ import annotations

import re
from pathlib import Path

from tightrein.config.project import ROOT_KEY, ConfigError, ConfigIssue
from tightrein.domain.normalize import Rule
from tightrein.store.files import yaml_text

RULES_KEY = "rules"
RULE_KEYS = frozenset({"pattern", "replacement"})


def _issues(data: object) -> list[ConfigIssue]:
    if data is None:
        return []
    if not isinstance(data, dict) or set(data) != {RULES_KEY} or not isinstance(data[RULES_KEY], list):
        return [ConfigIssue(ROOT_KEY, f"只能有一个顶层键 {RULES_KEY}，其值为规则列表")]
    issues = []
    for number, item in enumerate(data[RULES_KEY]):
        key = f"{RULES_KEY}[{number}]"
        if not isinstance(item, dict) or set(item) != RULE_KEYS:
            issues.append(ConfigIssue(key, "规则的键必须是 pattern、replacement"))
            continue
        if not isinstance(item["replacement"], str):
            issues.append(ConfigIssue(f"{key}.replacement", "必须是字符串"))
        if not isinstance(item["pattern"], str) or not item["pattern"]:
            issues.append(ConfigIssue(f"{key}.pattern", "必须是非空字符串"))
            continue
        try:
            re.compile(item["pattern"])
        except re.error as error:
            issues.append(ConfigIssue(f"{key}.pattern", f"正则无法编译：{error}"))
    return issues


def load(path: Path) -> tuple[Rule, ...]:
    if not path.exists():
        return ()
    try:
        data = yaml_text.load(path.read_text(encoding="utf-8"))
    except yaml_text.YamlError as error:
        raise ConfigError(path, [ConfigIssue(ROOT_KEY, str(error))]) from error
    issues = _issues(data)
    if issues:
        raise ConfigError(path, issues)
    return tuple(Rule(item["pattern"], item["replacement"]) for item in (data or {}).get(RULES_KEY, []))
