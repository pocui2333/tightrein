"""分支、提交与 PR 的通用格式，项目约定的识别优先级与从历史推断(redesign/07-release.md 第 1 节)。"""

import json

import pytest

from tightrein.domain import release_format
from tightrein.domain.enums import Severity, TaskType, Treatment
from tightrein.domain.release_format import FormatError, PullText
from tightrein.pipeline.common import conventions

TYPES = {"default": "bugfix", "feature": "feature", "refactor": "chore"}
PULL = PullText("导出时同一主问题拆成两条。", "分组 key 改为主问题，改动最小。", "旧数据需要重新导出。",
                ("Closes #3",), ("复现测试 `tests/test_export.py`：修复前失败，修复后验证通过",))


def test_generic_formats():
    kind = release_format.branch_type(TaskType.BUG, Treatment.SCHEDULED, Severity.P2, TYPES)
    assert release_format.branch(release_format.GENERIC_BRANCH, kind=kind, issue_id="0003",
                                 slug="Export Duplicate_Key") == "bugfix/3-export-duplicate-key"
    assert release_format.branch_type(TaskType.FEATURE, Treatment.IMMEDIATE, Severity.P0, TYPES) == "hotfix"
    assert release_format.branch_type(None, None, None, TYPES) == "bugfix"
    pattern = release_format.branch_pattern(release_format.GENERIC_BRANCH)
    assert pattern.match("bugfix/3-export-duplicate-key") and pattern.match("cty/feature/12-x")
    assert not pattern.match("cty/fix-export") and not pattern.match("Bugfix/3-x")
    message = release_format.commit_message(release_format.GENERIC_COMMIT, kind="fix", scope="导出",
                                            summary="同一主问题不同追问不再拆成两条", why="分组 key 含追问。")
    assert message == "fix(导出): 同一主问题不同追问不再拆成两条\n\n分组 key 含追问。\n"
    assert release_format.commit_message(release_format.GENERIC_COMMIT, kind="chore", scope=None, summary="x",
                                         why=None) == "chore: x\n"
    with pytest.raises(FormatError, match="title"):
        release_format.check_format("{type}: {title}", release_format.COMMIT_PLACEHOLDERS)


def test_the_generic_pr_body_has_no_fixed_sections_and_ends_with_the_verification():
    body = release_format.pull_body(PULL, "验证结果(程序生成)：")
    assert "## " not in body
    assert body.index("导出时") < body.index("分组 key") < body.index("旧数据") < body.index("Closes #3") < body.index(
        "验证结果")


def test_a_template_receives_the_same_content_under_its_headings():
    template = ("## 背景\n\n<!-- 说明问题 -->\n\n## 方案\n\n## 检查清单\n\n- [ ] 已更新文档\n\n## 测试\n\n")
    body = release_format.fill_template(template, PULL, "验证结果(程序生成)：")
    assert "## 背景\n\n导出时同一主问题拆成两条。\n\n旧数据需要重新导出。" in body
    assert "## 方案\n\n分组 key 改为主问题，改动最小。" in body and "## 检查清单\n\n- [ ] 已更新文档" in body
    assert "## 测试\n\n- 复现测试" in body and body.rstrip().endswith("Closes #3")
    plain = release_format.fill_template("请按团队规范填写。\n", PULL, "验证结果(程序生成)：")
    assert plain.startswith("请按团队规范填写。\n\n导出时")


def test_conventions_follow_the_priority(tmp_path, make_config):
    found = conventions.resolve(make_config(), tmp_path)
    assert (found.branch, found.commit, found.template, found.personal_prefix) == (
        release_format.GENERIC_BRANCH, release_format.GENERIC_COMMIT, None, False)
    assert found.sources == {"branch": "generic", "commit": "generic", "template": "generic"}
    (tmp_path / "CONTRIBUTING.md").write_text(
        "# 贡献\n\n分支命名：`<个人前缀>/<类型>-<简述>`，例如 `cty/fix-export`。\n"
        "提交信息第一行：`<类型>(<范围>): <描述>`\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "pull_request_template.md").write_text("## Summary\n", encoding="utf-8")
    found = conventions.resolve(make_config(), tmp_path)
    assert (found.branch, found.commit, found.personal_prefix) == ("{prefix}{type}-{slug}", "{type}{scope}: {summary}",
                                                                   True)
    assert found.sources["branch"] == "project:CONTRIBUTING.md:3" and found.template == "## Summary\n"
    workspace = conventions.resolve(make_config(git={"conventions": {"branch": "{type}/{slug}"}}), tmp_path)
    assert (workspace.branch, workspace.sources["branch"]) == ("{type}/{slug}", "workspace")


def test_unclear_documents_are_not_adopted(tmp_path, make_config):
    (tmp_path / "AGENTS.md").write_text("分支用 `<类型>/<简述>`\n另一种分支写法 `<类型>-<简述>`\n"
                                        "提交写 `<心情>: <描述>`\n", encoding="utf-8")
    found = conventions.resolve(make_config(), tmp_path)
    assert (found.branch, found.commit) == (release_format.GENERIC_BRANCH, release_format.GENERIC_COMMIT)
    (tmp_path / "package.json").write_text(json.dumps({"commitlint": {"extends": ["@commitlint/config-conventional"]}}),
                                           encoding="utf-8")
    assert conventions.resolve(make_config(), tmp_path).sources["commit"] == "project:package.json"


def test_history_inference_only_reports_a_uniform_style():
    subjects = [f"feat(x): 第 {n} 个" for n in range(9)] + ["Merge pull request #1", "随手修改"]
    branches = ["main", *(f"cty/fix-item-{n}" for n in range(8)), "cty/feat-new", "release-2026"]
    found = conventions.infer(subjects, branches, min_ratio=0.8, min_samples=5)
    assert found.commit.format == release_format.GENERIC_COMMIT and found.commit.ratio == 0.9
    assert found.branch.format == "{prefix}{type}-{slug}" and found.branch.samples[0] == "cty/fix-item-0"
    mixed = conventions.infer(["a", "b", "fix: c"], ["x/y", "z"], min_ratio=0.8, min_samples=5)
    assert (mixed.commit.format, mixed.branch.format) == (None, None)
    assert set(mixed.to_dict()) == {"commit", "branch"}
