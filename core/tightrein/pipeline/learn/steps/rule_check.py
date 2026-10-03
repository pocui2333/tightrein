"""用原补丁验证一条 Semgrep 规则(redesign/08-learn.md「缺陷变规则」，参照 KNighter)。

1. 规则文件只含一条规则，带 id、message、languages 与至少一个模式键；
2. 修复前版本(改动文件在基准 commit 上的内容)至少命中一处；
3. 修复后版本(改动文件在合并提交上的内容)不命中；
4. 在只读 worktree 全仓库运行，命中数不超过 learn.rules.maxRepoHits(误报的上限，不区分真实同类与误报)。
前三项在临时目录中运行；任何一次 Semgrep 失败都判为不通过并写明原因。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tightrein.sources.common.procs import Launcher, ToolCommand, tool_env
from tightrein.sources.static.tools import semgrep

RULE_FILE = "rule.yaml"
PATTERN_KEYS = frozenset({"pattern", "patterns", "pattern-either", "pattern-regex", "pattern-sources"})
REQUIRED_KEYS = ("id", "message", "languages")


@dataclass(frozen=True)
class RuleCheckSettings:
    command: str
    timeout_seconds: float
    max_repo_hits: int
    environ: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleCheck:
    accepted: bool
    reason: str | None
    rule_id: str | None = None
    before_hits: int | None = None
    after_hits: int | None = None
    repo_hits: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {"accepted": self.accepted, "reason": self.reason, "ruleId": self.rule_id,
                "beforeHits": self.before_hits, "afterHits": self.after_hits, "repoHits": self.repo_hits}


def rule_id(text: str) -> tuple[str | None, str | None]:
    """返回(规则编号, 不合格的原因)。"""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return None, f"规则不是合法的 YAML：{str(error).splitlines()[0]}"
    rules = document.get("rules") if isinstance(document, dict) else None
    if not isinstance(rules, list) or len(rules) != 1 or not isinstance(rules[0], dict):
        return None, "规则文件必须在 rules 下恰好有一条规则"
    rule = rules[0]
    missing = [key for key in REQUIRED_KEYS if not rule.get(key)]
    if missing:
        return None, f"规则缺少 {'、'.join(missing)}"
    if not PATTERN_KEYS & set(rule):
        return None, "规则没有模式(pattern、patterns、pattern-either、pattern-regex 或 pattern-sources)"
    return str(rule["id"]), None


def _write_tree(root: Path, files: Mapping[str, str | None]) -> int:
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    for path, text in files.items():
        if text is None:
            continue
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written += 1
    return written


def _hits(launcher: Launcher, settings: RuleCheckSettings, rule: Path, cwd: Path) -> tuple[int | None, str | None]:
    result = launcher(ToolCommand(semgrep.build_argv([str(rule)], ["."], settings.command), cwd,
                                  settings.timeout_seconds, tool_env(settings.environ, semgrep.ENVIRONMENT)))
    if result.exit_code != 0:
        return None, f"Semgrep 失败：{result.describe()}"
    try:
        findings, errors = semgrep.parse(json.loads(result.stdout))
    except (ValueError, KeyError):
        return None, "Semgrep 的输出不是预期的 JSON"
    if errors:
        return None, f"Semgrep 报告了错误：{'；'.join(errors)}"
    return len(findings), None


def check(launcher: Launcher, settings: RuleCheckSettings, text: str, before: Mapping[str, str | None],
          after: Mapping[str, str | None], repo: Path, work: Path) -> RuleCheck:
    """before、after 为改动文件(仓库内的相对路径)在修复前后的内容，文件不存在时为 None；work 为本次验证的临时目录。"""
    found, problem = rule_id(text)
    if problem is not None:
        return RuleCheck(False, problem)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    rule = work / RULE_FILE
    rule.write_text(text, encoding="utf-8")
    if not _write_tree(work / "before", before):
        return RuleCheck(False, "修复前没有可扫描的文件", found)
    before_hits, problem = _hits(launcher, settings, rule, work / "before")
    if problem is not None:
        return RuleCheck(False, f"修复前版本：{problem}", found)
    if not before_hits:
        return RuleCheck(False, "修复前的代码没有命中，规则没有抓住这个缺陷", found, 0)
    _write_tree(work / "after", after)
    after_hits, problem = _hits(launcher, settings, rule, work / "after")
    if problem is not None:
        return RuleCheck(False, f"修复后版本：{problem}", found, before_hits)
    if after_hits:
        return RuleCheck(False, f"修复后的代码仍命中 {after_hits} 处", found, before_hits, after_hits)
    repo_hits, problem = _hits(launcher, settings, rule, repo)
    if problem is not None:
        return RuleCheck(False, f"全仓库：{problem}", found, before_hits, after_hits)
    if repo_hits is not None and repo_hits > settings.max_repo_hits:
        return RuleCheck(False, f"全仓库命中 {repo_hits} 处，超过上限 {settings.max_repo_hits}(learn.rules.maxRepoHits)",
                         found, before_hits, after_hits, repo_hits)
    return RuleCheck(True, None, found, before_hits, after_hits, repo_hits)
