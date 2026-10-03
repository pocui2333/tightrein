"""gh 的只读查询(architecture/02 4.3，design 7.6、7.7)：PR 的状态、合并条件、必需检查的状态、描述、按分支与提交查 PR、
主分支的必需检查。
部署记录不在这里读取，见扩展点 deploy-source 与 pipeline/common/deploys.py。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tightrein.vcs import parse
from tightrein.vcs.errors import GhCommandError
from tightrein.vcs.parse import MergeFacts, PullState
from tightrein.vcs.process import VcsProcess

PR_FIELDS = "number,url,state,mergeable,mergedAt,mergeCommit,closedAt,reviewDecision,headRefName,comments,reviews"
MERGE_FIELDS = "state,isDraft,mergeable,mergeStateStatus,headRefOid,reviews"
CHECK_FIELDS = "name,state,bucket"
OPEN = "OPEN"
REQUIRED_STATUS_CHECKS = "required_status_checks"
FORBIDDEN = re.compile(r"\(HTTP 403\)\s*$")


class GhReader:
    def __init__(self, process: VcsProcess) -> None:
        self.process = process

    def required_checks(self, repo: Path, slug: str, branch: str) -> bool:
        """主分支是否要求必需检查：分支保护(repos/<仓库>/branches/<分支> 的 protection.required_status_checks)或规则集
        (repos/<仓库>/rules/branches/<分支> 中的 required_status_checks)。只读，读者有仓库读权限即可。"""
        found = json.loads(self.process.gh(repo, "api", f"repos/{slug}/branches/{branch}", retry=True).stdout or "{}")
        checks = ((found.get("protection") or {}).get("required_status_checks") or {})
        if found.get("protected") and (checks.get("contexts") or checks.get("checks")):
            return True
        try:
            text = self.process.gh(repo, "api", f"repos/{slug}/rules/branches/{branch}", retry=True).stdout
        except GhCommandError as error:
            # 免费账号的私有仓库没有规则集功能，接口返回 403；gh 只在错误输出末尾给出状态码
            if FORBIDDEN.search(error.stderr or ""):
                return False
            raise
        return any(rule.get("type") == REQUIRED_STATUS_CHECKS for rule in json.loads(text or "[]"))

    def pr_checks(self, repo: Path, number: int) -> list[dict[str, Any]]:
        """PR 的必需检查(name、state、bucket)；--json 时退出码不反映检查结果，由调用方按 bucket 判断。"""
        text = self.process.gh(repo, "pr", "checks", str(number), "--required", "--json", CHECK_FIELDS,
                               retry=True).stdout
        return list(json.loads(text or "[]"))

    def pr_view(self, repo: Path, ref: str) -> PullState:
        text = self.process.gh(repo, "pr", "view", ref, "--json", PR_FIELDS, retry=True).stdout
        return parse.parse_pull(json.loads(text))

    def pr_merge_facts(self, repo: Path, number: int) -> MergeFacts:
        """自动合并的判断所需的字段：开关状态、草稿、能否合并、合并状态(含必需检查与评审)、头部 commit 与评审。"""
        text = self.process.gh(repo, "pr", "view", str(number), "--json", MERGE_FIELDS, retry=True).stdout
        return parse.parse_merge_facts(json.loads(text))

    def pr_body(self, repo: Path, number: int) -> str:
        """PR 当前的描述；构造提 PR 操作时据此判断描述是否有变化。"""
        text = self.process.gh(repo, "pr", "view", str(number), "--json", "body", retry=True).stdout
        return json.loads(text)["body"]

    def pr_for_branch(self, repo: Path, branch: str) -> PullState | None:
        """该分支已有的 PR：有打开的取打开的，否则取编号最大的一个；没有时为 None。"""
        text = self.process.gh(repo, "pr", "list", "--head", branch, "--state", "all", "--json", "number,url,state",
                               retry=True).stdout
        pulls = sorted(parse.parse_pulls(text), key=lambda pull: (pull.state == OPEN, pull.number))
        return pulls[-1] if pulls else None

    def pr_for_commit(self, repo: Path, commit: str) -> PullState | None:
        text = self.process.gh(repo, "pr", "list", "--search", commit, "--state", "merged", "--json",
                               "number,url,state,mergedAt", retry=True).stdout
        pulls = parse.parse_pulls(text)
        return pulls[0] if pulls else None
