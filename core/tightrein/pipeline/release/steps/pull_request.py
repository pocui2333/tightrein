"""提 PR(architecture/07 19.4、19.5，redesign/07-release.md 第 1 节)：分支名复核与建议名、标题、描述的内容。

标题取 fix-executor 给出的 release.prTitle(一句祈使语气的完整话)，旧修复没有时取 Issue 标题。描述不调用模型：
正文三段(解决什么问题、为什么这样做、局限)取 release.problem、approach、limitations，没有时问题取 Issue 的「问题」、
做法取修复摘要；之后是关联(`Closes #<编号>`、父 Issue 与排在前面的子任务)；最后是程序生成的验证结果：复现测试修复前
与修复后的结果、修复后的项目检查、合并前验证的各项、照常提交时接受的未通过项。项目有 PR 模板时按模板的标题放进同样的
内容(domain/release_format.fill_template)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain import issue_sections as section_keys
from tightrein.domain import release_format
from tightrein.domain.issue import Issue
from tightrein.domain.release_format import PullText
from tightrein.pipeline.common.conventions import Conventions
from tightrein.pipeline.fix.steps import workspace

NONE = "无"
VERIFICATION_TITLE = "验证结果(程序生成)："
RESULTS = {"pass": "验证通过", "weak": "弱证据", "unverified": "未验证", "fail": "失败"}
PREFIX_PLACEHOLDER = "<个人前缀>"
BASE_RESULTS = {"failed": "失败", "passed": "通过"}


def branch_problem(branch: str, conventions: Conventions, config: ProjectConfig) -> str | None:
    if conventions.branch_pattern.fullmatch(branch) is None:
        return f"分支名 {branch} 不符合本项目的分支格式 {conventions.branch}"
    prefix = branch.split("/", 1)[0].lower()
    if conventions.personal_prefix and workspace.forbidden(prefix, config):
        return f"分支前缀 {prefix} 是 AI 或工具名称"
    return None


def suggest(issue: Issue, prefix: str | None, conventions: Conventions, config: ProjectConfig) -> str:
    """建议的分支名；项目要求个人前缀而没有配置或使用了禁用名称时留给用户填写。"""
    usable = prefix if prefix and not workspace.forbidden(prefix, config) else PREFIX_PLACEHOLDER
    kind = release_format.branch_type(issue.task_type, issue.treatment, issue.severity, config.get("git.branchTypes"))
    return release_format.branch(conventions.branch, kind=kind, issue_id=issue.id, slug=issue.slug,
                                 prefix=usable if conventions.personal_prefix else None)


def title(issue: Issue, fix: Mapping[str, Any]) -> str:
    return ((fix.get("release") or {}).get("prTitle") or issue.title).strip()


def _exit(code: int) -> str:
    return "通过" if code == 0 else f"退出码 {code}"


def verification(fix: Mapping[str, Any], repro: Mapping[str, Any] | None, local: Mapping[str, Any] | None,
                 accepted: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    if repro is not None and repro.get("file"):
        after = [item["afterFix"] for item in fix.get("reproCheck") or [] if item["kind"] == "test"]
        lines.append(f"复现测试 `{repro['file']}`：修复前{BASE_RESULTS.get(repro.get('baseResult') or '', '—')}，"
                     f"修复后{RESULTS.get(after[-1], after[-1]) if after else '—'}")
    elif repro is not None:
        lines.append(f"复现测试：未写({repro.get('reason') or NONE})")
    lines += [f"项目检查 {item['name']}：{_exit(item['exitCode'])}" for item in fix.get("checks") or []]
    if local is not None:
        items = local["items"]
        lines.append(f"合并前验证：{'通过' if local['conclusion'] == 'passed' else local['conclusion']}")
        others = [item for item in items if item["category"] == "other-repro"]
        if others:
            passed = sum(1 for item in others if item["result"] == "pass")
            lines.append(f"其他 Issue 的复现检查：{len(others)} 条，{passed} 条通过")
        lines += [f"{'接口浅跑' if item['category'] == 'api-shallow' else '页面巡检'}：{RESULTS[item['result']]}"
                  + (f"({item['reason']})" if item["reason"] else "") for item in items
                  if item["category"] in ("api-shallow", "page-patrol")]
        lines += [f"弱证据：{item['id']}({item['reason'] or ''})" for item in items if item["result"] == "weak"]
        lines += [f"未验证：{item['item']}({item['reason']})" for item in local["unverified"]]
    lines += [f"照常提交时接受的未通过项：{item['check']} {item['location'] or ''} {item['problem']}".replace("  ", " ")
              for item in accepted]
    return lines


def text(issue_text: Mapping[str, str], fix: Mapping[str, Any], relations: Sequence[str],
         checks: Sequence[str]) -> PullText:
    written = fix.get("release") or {}
    problem = written.get("problem") or (section_keys.find(issue_text, section_keys.PROBLEM) or "").strip() or NONE
    approach = written.get("approach") or fix.get("summary") or ""
    return PullText(problem, approach, written.get("limitations") or "", tuple(relations), tuple(checks))


def body(conventions: Conventions, pull: PullText) -> str:
    if conventions.template:
        return release_format.fill_template(conventions.template, pull, VERIFICATION_TITLE)
    return release_format.pull_body(pull, VERIFICATION_TITLE)
