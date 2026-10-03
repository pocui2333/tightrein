from datetime import datetime, timezone
from pathlib import Path

import pytest

from tightrein.vcs.errors import GhAuthError, GhCommandError
from tightrein.vcs.gh_read import GhReader
from tightrein.vcs.process import Completed, VcsProcess

FIXTURES = Path(__file__).parent / "fixtures" / "gh"


class RecordedGh:
    """按命令返回录制的 gh 输出；routes 为 (参数片段, 夹具文件名、退出码或 (退出码, 错误输出))。"""

    def __init__(self, *routes):
        self.routes = routes
        self.commands = []

    def __call__(self, command):
        self.commands.append(command.argv)
        for fragment, outcome in self.routes:
            if all(part in command.argv for part in fragment):
                if isinstance(outcome, int):
                    return Completed(command.argv, outcome, "", "gh: authentication required\n")
                if isinstance(outcome, tuple):
                    return Completed(command.argv, outcome[0], "", outcome[1])
                return Completed(command.argv, 0, (FIXTURES / outcome).read_text(encoding="utf-8"))
        raise AssertionError(f"没有录制的输出：{command.argv}")


def reader(tmp_path, gh):
    return GhReader(VcsProcess(execute=gh, environ={}, sleep=lambda seconds: None))


def test_required_checks_come_from_branch_protection_or_rulesets(tmp_path):
    protected = RecordedGh((("repos/cty/sample/branches/main",), "branch-protected.json"))
    assert reader(tmp_path, protected).required_checks(tmp_path, "cty/sample", "main") is True
    ruled = RecordedGh((("repos/cty/sample/branches/main",), "branch-unprotected.json"),
                       (("repos/cty/sample/rules/branches/main",), "rules-required-checks.json"))
    assert reader(tmp_path, ruled).required_checks(tmp_path, "cty/sample", "main") is True
    plain = RecordedGh((("repos/cty/sample/branches/main",), "branch-unprotected.json"),
                       (("repos/cty/sample/rules/branches/main",), "rules-none.json"))
    assert reader(tmp_path, plain).required_checks(tmp_path, "cty/sample", "main") is False
    upgrade = "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)\n"
    private = RecordedGh((("repos/cty/sample/branches/main",), "branch-unprotected.json"),
                         (("repos/cty/sample/rules/branches/main",), (1, upgrade)))
    assert reader(tmp_path, private).required_checks(tmp_path, "cty/sample", "main") is False
    broken = RecordedGh((("repos/cty/sample/branches/main",), "branch-unprotected.json"),
                        (("repos/cty/sample/rules/branches/main",), (1, "gh: Not Found (HTTP 404)\n")))
    with pytest.raises(GhCommandError):
        reader(tmp_path, broken).required_checks(tmp_path, "cty/sample", "main")


def test_pull_request_view(tmp_path):
    gh = RecordedGh((("pr", "view"), "pr-view.json"))
    pull = reader(tmp_path, gh).pr_view(tmp_path, "186")
    assert (pull.number, pull.state, pull.review_decision, pull.merge_commit) == (186, "MERGED", "APPROVED", "e" * 40)
    assert pull.merged_at == datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
    assert pull.comments[0]["body"] == "分页上限改成 200 之后再合并"
    assert gh.commands[0][:4] == ("gh", "pr", "view", "186")


def test_pull_request_body(tmp_path):
    gh = RecordedGh((("--json", "body"), "pr-view-body.json"))
    assert reader(tmp_path, gh).pr_body(tmp_path, 186).startswith("## 1. 问题")
    assert gh.commands[0] == ("gh", "pr", "view", "186", "--json", "body")


def test_pull_request_for_branch_prefers_the_open_one(tmp_path):
    gh = RecordedGh((("--head", "cty/fix-order-query-500"), "pr-list-branch.json"),
                    (("--head", "cty/fix-other"), "pr-list-empty.json"))
    git = reader(tmp_path, gh)
    assert git.pr_for_branch(tmp_path, "cty/fix-order-query-500").number == 186
    assert git.pr_for_branch(tmp_path, "cty/fix-other") is None


def test_pull_request_for_commit(tmp_path):
    gh = RecordedGh((("--search", "e" * 40), "pr-list-commit.json"), (("--search", "f" * 40), "pr-list-empty.json"))
    git = reader(tmp_path, gh)
    assert git.pr_for_commit(tmp_path, "e" * 40).number == 186
    assert git.pr_for_commit(tmp_path, "f" * 40) is None


def test_auth_errors_are_raised(tmp_path):
    gh = RecordedGh((("pr", "view"), 4))
    with pytest.raises(GhAuthError):
        reader(tmp_path, gh).pr_view(tmp_path, "186")
