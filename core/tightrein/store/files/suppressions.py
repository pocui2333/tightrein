"""suppressions.yaml 的校验、读取与带文件锁、带备份的写入(design 2.9，architecture/05 3.5)。

文件只有一个顶层键 rules，每条规则的结构与 SuppressionRule.to_dict 相同，日期为字符串。聚合的 false-positive、
分诊判为误报、以「不是缺陷」关闭 Issue 共用 add_rule：在文件锁内先校验现有文件，再备份到 data/archive/，
写入后重新读取校验，校验不一致时恢复备份。
"""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.suppression import SuppressionRule
from tightrein.store import locks
from tightrein.store.files import atomic, yaml_text
from tightrein.store.files.layout import WorkspaceLayout

RULES_KEY = "rules"
_RULE_KEYS = frozenset({"match", "reason", "addedOn", "expiresOn"})
_MATCH_KEYS = (frozenset({"fingerprint"}), frozenset({"probe", "messagePattern"}))


class SuppressionFileError(ValueError):
    """suppressions.yaml 不合格，或写入后读回的内容与写入的不一致。"""


def _rule(item: Any) -> SuppressionRule:
    if not isinstance(item, dict):
        raise ValueError("规则必须是映射")
    if set(item) != _RULE_KEYS:
        raise ValueError(f"规则的键必须是 {', '.join(sorted(_RULE_KEYS))}，实际为 {', '.join(sorted(item))}")
    match = item["match"]
    if not isinstance(match, dict) or frozenset(match) not in _MATCH_KEYS:
        raise ValueError("match 只能是 fingerprint，或者 probe 加 messagePattern")
    for key in ("reason", "addedOn", "expiresOn"):
        if not isinstance(item[key], str) or not item[key]:
            raise ValueError(f"{key} 必须是非空字符串")
    for value in match.values():
        if not isinstance(value, str) or not value:
            raise ValueError("match 中的值必须是非空字符串")
    for key in ("addedOn", "expiresOn"):
        date.fromisoformat(item[key])
    return SuppressionRule.from_dict(item)


def parse(text: str, source: str = "suppressions.yaml") -> list[SuppressionRule]:
    """解析全部规则；任何一条不合格时列出全部不合格的规则并抛出 SuppressionFileError。"""
    try:
        data = yaml_text.load(text)
    except yaml_text.YamlError as error:
        raise SuppressionFileError(f"{source} 无法解析：{error}") from error
    if data is None:
        return []
    if not isinstance(data, dict) or set(data) != {RULES_KEY} or not isinstance(data[RULES_KEY], list):
        raise SuppressionFileError(f"{source} 只能有一个顶层键 {RULES_KEY}，其值为规则列表")
    rules: list[SuppressionRule] = []
    problems: list[str] = []
    for number, item in enumerate(data[RULES_KEY], start=1):
        try:
            rules.append(_rule(item))
        except (ValueError, re.error) as error:
            problems.append(f"第 {number} 条规则：{error}")
    if problems:
        raise SuppressionFileError(f"{source} 不合格：\n" + "\n".join(problems))
    return rules


def read(path: Path) -> list[SuppressionRule]:
    """文件不存在时没有规则。"""
    if not path.exists():
        return []
    return parse(path.read_text(encoding="utf-8"), str(path))


def render(rules: list[SuppressionRule]) -> str:
    return yaml_text.dump({RULES_KEY: [rule.to_dict() for rule in rules]})


def _backup(layout: WorkspaceLayout, clock: Clock) -> Path:
    now = clock.now()
    number = 0
    while (candidate := layout.suppressions_backup(now, number)).exists():
        number += 1
    return candidate


def add_rule(layout: WorkspaceLayout, rule: SuppressionRule, clock: Clock) -> list[SuppressionRule]:
    """追加一条规则，返回写入后的全部规则。按指纹匹配的规则与已有规则指纹相同时替换已有规则。"""
    path = layout.suppressions()
    with locks.file_lock(layout.suppressions_lock()):
        current = read(path)
        replaced = False
        rules: list[SuppressionRule] = []
        for existing in current:
            if rule.fingerprint is not None and existing.fingerprint == rule.fingerprint:
                rules.append(rule)
                replaced = True
            else:
                rules.append(existing)
        if not replaced:
            rules.append(rule)
        backup = None
        if path.exists():
            backup = _backup(layout, clock)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, backup)
        atomic.write_text(path, render(rules))
        try:
            written = read(path)
        except SuppressionFileError:
            written = None
        if written != rules:
            if backup is not None:
                shutil.copyfile(backup, path)
            else:
                path.unlink()
            raise SuppressionFileError(f"{path} 写入后读回的规则与写入的不一致，已恢复写入前的内容")
    return rules
