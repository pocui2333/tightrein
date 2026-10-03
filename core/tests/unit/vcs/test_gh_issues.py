"""GitHub Issue 镜像的 gh 调用：远程地址解析、只读查询的参数与解析、写命令的参数。"""

import json

import pytest

from tightrein.domain.issue import GithubLink
from tightrein.vcs import gh_issues
from tightrein.vcs.gh_issues import GhIssues, RemoteIssue, RepositoryFacts
from tightrein.vcs.process import Completed, VcsProcess


@pytest.mark.parametrize("url, slug", [
    ("git@github.com:owner/name.git", "owner/name"),
    ("https://github.com/owner/name", "owner/name"),
    ("https://github.com/owner/name.git/", "owner/name"),
    ("ssh://git@github.com/owner/my.repo.git", "owner/my.repo"),
    ("https://gitlab.com/owner/name.git", None),
])
def test_repo_slug(url, slug):
    assert gh_issues.repo_slug(url) == slug


def test_issue_link_takes_the_last_issue_url():
    out = "Creating issue in owner/name\n\nhttps://github.com/owner/name/issues/12\n"
    assert gh_issues.issue_link(out) == GithubLink(12, "https://github.com/owner/name/issues/12")
    with pytest.raises(ValueError, match="没有 Issue 链接"):
        gh_issues.issue_link("")


class Replies:
    def __init__(self, payload):
        self.payload = payload
        self.commands = []

    def __call__(self, command):
        self.commands.append(command.argv)
        return Completed(command.argv, 0, json.dumps(self.payload))


def reader(tmp_path, payload):
    replies = Replies(payload)
    return GhIssues(VcsProcess(execute=replies, environ={}, sleep=lambda seconds: None), tmp_path, 50), replies


def test_read_queries(tmp_path):
    issues, replies = reader(tmp_path, {"isPrivate": True, "hasIssuesEnabled": True})
    assert issues.repository("owner/name") == RepositoryFacts(True, True)
    assert replies.commands[0] == ("gh", "repo", "view", "owner/name", "--json", "isPrivate,hasIssuesEnabled")
    issues, replies = reader(tmp_path, [{"number": 3, "state": "CLOSED", "stateReason": "NOT_PLANNED"},
                                        {"number": 4, "state": "OPEN", "stateReason": ""}])
    assert issues.states("owner/name") == {3: RemoteIssue(3, "closed", "NOT_PLANNED"), 4: RemoteIssue(4, "open")}
    assert replies.commands[0][-2:] == ("--limit", "50") and "--state" in replies.commands[0]
    issues, _ = reader(tmp_path, [{"name": "tightrein:todo"}])
    assert issues.labels("owner/name") == {"tightrein:todo"}
    issues, _ = reader(tmp_path, [{"number": 9, "url": "u9", "body": "x <!-- m -->"},
                                  {"number": 5, "url": "u5", "body": "<!-- m -->"}, {"number": 1, "url": "u1"}])
    assert issues.find_marker("owner/name", "<!-- m -->") == GithubLink(5, "u5")


def test_write_arguments():
    assert gh_issues.create_args("o/n", "标题", "/r/body.md", ["a", "b"]) == (
        "issue", "create", "--repo", "o/n", "--title", "标题", "--body-file", "/r/body.md", "--label", "a",
        "--label", "b")
    assert gh_issues.edit_labels_args("o/n", 3, ["a"], ["b", "c"]) == (
        "issue", "edit", "3", "--repo", "o/n", "--add-label", "a", "--remove-label", "b,c")
    assert gh_issues.close_args("o/n", 3, gh_issues.NOT_PLANNED)[-2:] == ("--reason", "not planned")
    assert gh_issues.reopen_args("o/n", 3) == ("issue", "reopen", "3", "--repo", "o/n")
