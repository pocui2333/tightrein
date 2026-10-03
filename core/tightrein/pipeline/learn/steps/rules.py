"""缺陷变规则(redesign/08-learn.md 第 1 节)：修好一个缺陷后由 rule-writer 写一条 Semgrep 规则，用原补丁验证后收入规则库。

处理 learn.rules.lookbackDays 天内以已修复关闭、不是用户需求、幂等键 rule:<Issue> 未完成的 Issue：
1. 取修复交接文档的 baseCommit 与 changedFiles(去掉 testPaths 匹配的文件)、PR 的合并提交与两者之间的改动；
   缺少材料时记为不生成规则(写入幂等键，不再处理)；
2. 运行 rule-writer(经执行器，环节效益照常登记)；执行器没有结果时记入 errors，下次重试；
3. 规则经 rule_check 验证：通过的写入工作区 rules/<Issue>-<简称>.yaml(头部注释写来源与验证数字)，静态巡检读取规则库；
4. 规则表达不了或验证不通过：不收录并写明原因，缺陷模式描述作为 defect-pattern 条目写入知识库(经写入去重，带来源与
   复核日期)，由 variant-scan 在全量巡检中按描述查找；
5. 结果存进幂等键，之后不再处理该 Issue。--output 模式不运行。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tightrein.domain.enums import KnowledgeType, RunnerStatus, RunStage
from tightrein.guards.policy import GuardSettings
from tightrein.guards.protected import matching_pattern
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.learn.prompts.common import LearnEnv
from tightrein.pipeline.learn.prompts.rule import rule_task
from tightrein.pipeline.learn.steps import rule_check
from tightrein.pipeline.learn.steps.metric_base import fixed_closes
from tightrein.pipeline.learn.steps.rule_check import RuleCheckSettings
from tightrein.retrieval.errors import KnowledgeError
from tightrein.retrieval.writer import KnowledgeDraft
from tightrein.runner.task import Subject
from tightrein.sources.common.procs import Launcher
from tightrein.store import idempotency
from tightrein.store.files import atomic
from tightrein.store.repos import issues, pulls
from tightrein.vcs.errors import VcsError
from tightrein.vcs.git_read import GitReader

KEY_PREFIX = "rule:"
RULE_SUFFIX = ".yaml"
PATTERN_SECTIONS = (("现象", "phenomenon"), ("根因写法", "rootCausePattern"), ("怎么检出", "detection"))


@dataclass(frozen=True)
class RuleEnv:
    """git 读取修复前后的文件；repo 为被测项目仓库，worktree 为全仓库扫描用的只读 worktree。"""

    git: GitReader
    launcher: Launcher
    settings: RuleCheckSettings
    repo: Path
    worktree: Path


@dataclass
class RuleReport:
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


def key(issue_id: str) -> str:
    return f"{KEY_PREFIX}{issue_id}"


def _done(env: LearnEnv, issue_id: str) -> bool:
    record = idempotency.get(env.conn, key(issue_id))
    return record is not None and record.status == idempotency.DONE


def library(directory: Path) -> list[Path]:
    """规则库中的规则文件，按文件名排序。"""
    return sorted(directory.glob(f"*{RULE_SUFFIX}")) if directory.is_dir() else []


def _read(path: Path | None) -> str:
    return path.read_text(encoding="utf-8") if path is not None and path.is_file() else ""


@dataclass(frozen=True)
class FixFacts:
    base: str
    merge: str
    files: tuple[str, ...]
    patch: str


def _facts(env: LearnEnv, rule_env: RuleEnv, issue_id: str) -> tuple[FixFacts | None, str | None]:
    found = stage_runs.latest_outputs(env.conn, env.layout, RunStage.FIX, issue_id)
    pull = pulls.get(env.conn, issue_id)
    if found is None or not found[1].get("baseCommit"):
        return None, "没有修复交接文档"
    if pull is None or pull.merge_commit is None:
        return None, "没有合并提交"
    tests = GuardSettings.from_config(env.config).test_paths
    files = tuple(item["path"] for item in found[1].get("changedFiles") or []
                  if matching_pattern(item["path"], tests) is None)
    if not files:
        return None, "改动中没有测试之外的文件"
    try:
        diff = rule_env.git.diff(rule_env.repo, found[1]["baseCommit"], pull.merge_commit, list(files))
    except VcsError as error:
        return None, f"读不到修复前后的改动：{error}"
    patch = "\n\n".join("\n".join([f"--- {item.path}", *(f"-{line}" for line in item.removed_lines),
                                    *(f"+{line}" for line in item.added_lines)]) for item in diff.files)
    return FixFacts(found[1]["baseCommit"], pull.merge_commit, files, patch), None


def _versions(rule_env: RuleEnv, facts: FixFacts) -> tuple[dict[str, str | None], dict[str, str | None]]:
    before = {path: rule_env.git.show(rule_env.repo, facts.base, path) for path in facts.files}
    after = {path: rule_env.git.show(rule_env.repo, facts.merge, path) for path in facts.files}
    return before, after


def _rule_file(env: LearnEnv, issue_id: str, slug: str) -> Path:
    return env.layout.rules_dir() / f"{issue_id}-{slug}{RULE_SUFFIX}"


def _header(issue_id: str, run_id: str, checked: rule_check.RuleCheck) -> str:
    return (f"# 来源：Issue {issue_id}(运行 {run_id})\n"
            f"# 验证：修复前命中 {checked.before_hits} 处，修复后命中 {checked.after_hits} 处，"
            f"全仓库命中 {checked.repo_hits} 处\n")


def pattern_draft(env: LearnEnv, issue_id: str, data: Mapping[str, Any], reason: str) -> KnowledgeDraft:
    parts = [f"## {title}\n\n{str(data[name]).strip()}" for title, name in PATTERN_SECTIONS]
    parts.append("## 反例\n\n" + "\n".join(f"- {item}" for item in data["counterExamples"]))
    parts.append("## 适用目录\n\n" + "\n".join(f"- {item}" for item in data["directories"]))
    parts.append(f"## 来源\n\n- Issue {issue_id} 修复后由 rule-writer 写出；没有收入规则库：{reason}\n- 运行：{env.run_id}")
    review_by = env.today() + timedelta(days=env.config.whole_threshold("learn.lessonReviewDays"))
    return KnowledgeDraft(KnowledgeType.DEFECT_PATTERN, data["slug"], data["title"], data["summary"],
                          tuple(data["tags"]), "\n\n".join(parts) + "\n", review_by, (), env.run_id)


def _fallback(env: LearnEnv, issue_id: str, output: Mapping[str, Any], reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {"issueId": issue_id, "accepted": False, "path": None, "reason": reason,
                              "knowledgeId": None}
    try:
        written = env.knowledge.write(pattern_draft(env, issue_id, output, reason), env.calls.runner,
                                      env.origin(Subject("issue", issue_id)))
    except KnowledgeError as error:
        result["reason"] = f"{reason}；缺陷模式写入失败：{error}"
    else:
        result["knowledgeId"] = written.written_id
    return result


def _one(env: LearnEnv, rule_env: RuleEnv, issue_id: str, rules: Sequence[Path]) -> dict[str, Any] | str:
    """返回结果(写入幂等键)，或下次需要重试时的原因。"""
    facts, problem = _facts(env, rule_env, issue_id)
    if facts is None:
        return {"issueId": issue_id, "accepted": False, "path": None, "reason": f"不生成规则：{problem}",
                "knowledgeId": None}
    record = issues.get(env.conn, issue_id)
    text = _read(env.layout.root / record.path) if record is not None else ""
    finding = _read(env.layout.root / record.issue.findings) if record is not None and record.issue.findings else ""
    task = rule_task(env.calls.prompt, issue_id, text, finding, _read(env.layout.fix_report(issue_id)), facts.patch,
                     "\n".join(f"- {path.stem}" for path in rules) or "(规则库为空)",
                     rule_env.worktree if rule_env.worktree.is_dir() else None)
    result = env.calls.run(task)
    if result.status is not RunnerStatus.OK or result.output is None:
        return f"rule-writer 没有给出结果：{result.status.value} {result.error_type or ''}".strip()
    output = dict(result.output)
    yaml_text = output["rule"]["yaml"]
    if not yaml_text:
        return _fallback(env, issue_id, output, f"规则表达不了：{output['rule']['reason'] or '没有说明'}")
    before, after = _versions(rule_env, facts)
    checked = rule_check.check(rule_env.launcher, rule_env.settings, yaml_text, before, after, rule_env.worktree,
                               env.layout.run_dir(env.run_id) / "rules" / issue_id)
    if not checked.accepted:
        found = _fallback(env, issue_id, output, f"验证不通过：{checked.reason}")
        return {**found, "check": checked.to_dict()}
    path = _rule_file(env, issue_id, output["slug"])
    atomic.write_text(path, _header(issue_id, env.run_id, checked) + yaml_text.rstrip() + "\n")
    return {"issueId": issue_id, "accepted": True, "path": env.layout.relative(path), "reason": None,
            "knowledgeId": None, "check": checked.to_dict()}


def generate(env: LearnEnv, rule_env: RuleEnv | None) -> RuleReport:
    report = RuleReport()
    if not env.writable or rule_env is None:
        return report
    now = env.clock.now()
    since = now - timedelta(days=env.config.whole_threshold("learn.rules.lookbackDays"))
    rules = library(env.layout.rules_dir())
    for issue_id in fixed_closes(env.conn, since, now):
        record = issues.get(env.conn, issue_id)
        if record is None or record.issue.is_manual or _done(env, issue_id):
            continue
        outcome = _one(env, rule_env, issue_id, rules)
        if isinstance(outcome, str):
            report.errors.append({"item": key(issue_id), "reason": outcome})
            continue
        idempotency.run_once(env.conn, key(issue_id), lambda: outcome, env.clock)
        report.results.append(outcome)
    return report


def recent(conn: sqlite3.Connection, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """[since, until) 内处理完的 Issue 的规则结果(周报列出)。"""
    return [dict(record.result) for record in idempotency.find(conn, KEY_PREFIX)
            if record.status == idempotency.DONE and record.result and record.completed_at is not None
            and since <= record.completed_at < until]
