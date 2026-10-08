"""GitHub：--repo 与 --body-file -、PR 先查后做且只在描述变化时更新、--match-head-commit 合并、必需检查、
只读才重试、Issue 同步(公开仓库拒绝、按标记找回、只移除存在的标签)。"""

import json

import pytest

from tightrein.protocol.git.git import AuthError, CommandFailed, NetworkError, Stale, idempotency_key
from tightrein.protocol.git.github import GitHub, NewIssue, PublicRepository, repo_slug
from tightrein.protocol.process import Command, Outcome

SLUG = "cty/sample"
PR_URL = "https://github.com/cty/sample/pull/{number}"
ISSUE_URL = "https://github.com/cty/sample/issues/{number}"


class FakeGh:
    """gh 的替身：按内存中的 PR 与 Issue 应答；failures 为 {(子命令, 动作): Outcome}，命中时先返回它一次。"""

    def __init__(self) -> None:
        self.pulls: dict[int, dict] = {}
        self.issues: dict[int, dict] = {}
        self.labels = {"tightrein:todo"}
        self.private = True
        self.commands: list[Command] = []
        self.failures: dict[tuple[str, str], list[Outcome]] = {}
        self.api: dict[str, tuple[int, str, str]] = {}

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        argv = list(command.argv)
        group, action = argv[1], argv[2]
        if self.failures.get((group, action)):
            return self.failures[(group, action)].pop(0)
        if group == "api":
            code, stdout, stderr = self.api[action]
            return Outcome(code, stdout, stderr, 1, None, None)
        reply = getattr(self, f"_{group}_{action}".replace("-", "_"))(argv, command.stdin)
        return Outcome(0, reply if isinstance(reply, str) else json.dumps(reply), "", 1, None, None)

    def _pr_list(self, argv, stdin):
        head = _value(argv, "--head")
        return [{"number": number, "url": PR_URL.format(number=number), "state": pull["state"]}
                for number, pull in self.pulls.items() if pull["head"] == head]

    def _pr_view(self, argv, stdin):
        number = int(argv[3])
        pull = self.pulls[number]
        return {"number": number, "url": PR_URL.format(number=number), "state": pull["state"], "body": pull["body"],
                "comments": [{"body": body, "url": f"{PR_URL.format(number=number)}#c{index}"}
                             for index, body in enumerate(pull.get("comments", []))]}

    def _pr_create(self, argv, stdin):
        number = max(self.pulls, default=185) + 1
        self.pulls[number] = {"head": _value(argv, "--head"), "state": "OPEN", "body": stdin}
        return PR_URL.format(number=number) + "\n"

    def _pr_edit(self, argv, stdin):
        self.pulls[int(argv[3])]["body"] = stdin
        return PR_URL.format(number=int(argv[3])) + "\n"

    def _pr_comment(self, argv, stdin):
        self.pulls[int(argv[3])].setdefault("comments", []).append(stdin)
        return PR_URL.format(number=int(argv[3])) + "#c\n"

    def _pr_merge(self, argv, stdin):
        self.pulls[int(argv[3])]["state"] = "MERGED"
        return ""

    def _pr_checks(self, argv, stdin):
        return [{"name": "test", "state": "FAILURE", "bucket": "fail"}]

    def _repo_view(self, argv, stdin):
        return {"isPrivate": self.private, "hasIssuesEnabled": True}

    def _label_list(self, argv, stdin):
        return [{"name": name} for name in sorted(self.labels)]

    def _issue_list(self, argv, stdin):
        return [{"number": number, "url": ISSUE_URL.format(number=number), "body": issue["body"],
                 "state": issue["state"].upper(), "stateReason": ""} for number, issue in self.issues.items()]

    def _issue_create(self, argv, stdin):
        number = len(self.issues) + 1
        self.issues[number] = {"body": stdin, "state": "open", "labels": []}
        return f"Creating issue in {SLUG}\n\n{ISSUE_URL.format(number=number)}\n"

    def _issue_edit(self, argv, stdin):
        return ISSUE_URL.format(number=int(argv[3])) + "\n"


def _value(argv, flag):
    return argv[argv.index(flag) + 1]


@pytest.fixture
def gh(tmp_path, settings):
    fake = FakeGh()
    sleeps = []
    client = GitHub(tmp_path, SLUG, fake, {"PATH": "/usr/bin"}, settings, sleep=sleeps.append)
    return client, fake, sleeps


@pytest.mark.parametrize("url, slug", [
    ("git@github.com:owner/name.git", "owner/name"),
    ("https://github.com/owner/name", "owner/name"),
    ("https://github.com/owner/name.git/", "owner/name"),
    ("ssh://git@github.com/owner/my.repo.git", "owner/my.repo"),
    ("https://gitlab.com/owner/name.git", None),
])
def test_repo_slug(url, slug):
    assert repo_slug(url) == slug


def test_every_command_names_the_repository_and_bodies_go_through_stdin(gh, scope):
    client, fake, _ = gh
    number = client.create_pr("cty/fix-order", "main", "Fix order query", "描述\n", scope=scope)
    created = fake.commands[-1]
    assert created.argv[-2:] == ("--repo", SLUG)
    assert created.argv[created.argv.index("--body-file") + 1] == "-" and created.stdin == "描述\n"
    assert "描述\n" not in created.argv
    assert created.env["GH_PROMPT_DISABLED"] == "1" and created.timeout_s == 300
    assert (number, fake.pulls[number]["body"]) == (186, "描述\n")


def test_pull_requests_are_created_once_and_updated_only_when_the_body_changes(gh, scope):
    client, fake, _ = gh
    first = client.create_pr("cty/fix-order", "main", "Fix", "描述\n", scope=scope)
    assert client.create_pr("cty/fix-order", "main", "Fix", "描述\n", scope=scope) == first
    edits = [command for command in fake.commands if command.argv[1:3] == ("pr", "edit")]
    creates = [command for command in fake.commands if command.argv[1:3] == ("pr", "create")]
    assert (len(creates), edits) == (1, [])
    assert client.create_pr("cty/fix-order", "main", "Fix", "新描述\n", scope=scope) == first
    assert fake.pulls[first]["body"] == "新描述\n"


def test_the_branch_lookup_prefers_the_open_pull_request(gh):
    client, fake, _ = gh
    fake.pulls = {180: {"head": "cty/fix-order", "state": "CLOSED", "body": ""},
                  190: {"head": "cty/fix-order", "state": "MERGED", "body": ""},
                  185: {"head": "cty/fix-order", "state": "OPEN", "body": ""}}
    assert client.pr_for_branch("cty/fix-order").number == 185
    del fake.pulls[185]
    assert client.pr_for_branch("cty/fix-order").number == 190
    assert client.pr_for_branch("cty/other") is None


def test_an_interrupted_create_does_not_open_a_second_pull_request(gh, scope, interrupt):
    """上次 gh pr create 做完但没来得及记完成：先查到该分支已有打开的 PR，就不再新建。"""
    client, fake, _ = gh
    key = idempotency_key(scope, "pr-create", ("cty/fix-order", "main", "Fix", "描述\n"))
    interrupt(key)
    fake.pulls[186] = {"head": "cty/fix-order", "state": "OPEN", "body": "描述\n"}
    assert client.create_pr("cty/fix-order", "main", "Fix", "描述\n", scope=scope) == 186
    assert not any(command.argv[1:3] == ("pr", "create") for command in fake.commands)


def test_a_pull_request_closed_after_the_decision_is_not_recreated(gh, scope):
    """做决定时该分支有打开的 PR(打算更新它)，执行前被关了：判过期，不另开一个新 PR。"""
    client, fake, _ = gh
    fake.pulls[186] = {"head": "cty/fix-order", "state": "OPEN", "body": "描述\n"}
    decided = client.pull_state("cty/fix-order")
    assert decided == {"pullRequest": "186"}
    fake.pulls[186]["state"] = "CLOSED"
    with pytest.raises(Stale, match="pullRequest"):
        client.create_pr("cty/fix-order", "main", "Fix", "描述\n", scope=scope, expected=decided)
    assert not any(command.argv[1:3] == ("pr", "create") for command in fake.commands)


def test_merging_matches_the_head_and_deletes_only_the_remote_branch(gh, scope):
    client, fake, _ = gh
    fake.pulls[186] = {"head": "cty/fix-order", "state": "OPEN", "body": ""}
    client.merge_pr(186, "a" * 40, "squash", scope=scope)
    assert fake.commands[-1].argv == ("gh", "pr", "merge", "186", "--squash", "--delete-branch",
                                      "--match-head-commit", "a" * 40, "--repo", SLUG)
    assert fake.pulls[186]["state"] == "MERGED"
    client.merge_pr(186, "b" * 40, "squash", auto=True, scope=scope)
    assert "--auto" in fake.commands[-1].argv


def test_review_comments_are_posted_once(gh, scope):
    client, fake, _ = gh
    fake.pulls[186] = {"head": "cty/fix-order", "state": "OPEN", "body": ""}
    client.comment(186, "AI 审查：通过", scope=scope)
    client.comment(186, "AI 审查：通过", scope=scope)
    assert fake.pulls[186]["comments"] == ["AI 审查：通过"]


def test_required_checks_come_from_branch_protection_or_rulesets(gh):
    client, fake, _ = gh
    branch, rules = f"repos/{SLUG}/branches/main", f"repos/{SLUG}/rules/branches/main"
    fake.api = {branch: (0, json.dumps({"protected": True, "protection": {"required_status_checks":
                                                                          {"contexts": ["ci"]}}}), "")}
    assert client.required_checks("main") is True
    fake.api = {branch: (0, json.dumps({"protected": False}), ""),
                rules: (0, json.dumps([{"type": "required_status_checks"}]), "")}
    assert client.required_checks("main") is True
    fake.api[rules] = (0, "[]", "")
    assert client.required_checks("main") is False
    upgrade = "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)\n"
    fake.api[rules] = (1, "", upgrade)
    assert client.required_checks("main") is False
    fake.api[rules] = (1, "", "gh: Not Found (HTTP 404)\n")
    with pytest.raises(CommandFailed):
        client.required_checks("main")
    assert all("--repo" not in command.argv for command in fake.commands)


def test_check_results_are_read_from_the_buckets_not_the_exit_code(gh):
    client, fake, _ = gh
    fake.failures[("pr", "checks")] = [Outcome(1, json.dumps([{"name": "test", "state": "FAILURE",
                                                               "bucket": "fail"}]), "", 1, None, None)]
    assert [(check.name, check.bucket) for check in client.pr_checks(186)] == [("test", "fail")]


def test_auth_errors_and_retries_only_for_reads(gh, scope):
    client, fake, sleeps = gh
    fake.failures[("pr", "view")] = [Outcome(4, "", "gh auth login\n", 1, None, None)]
    with pytest.raises(AuthError):
        client.pr_body(186)
    timeout = Outcome(None, "", "", 1, "timeout", None)
    fake.pulls[186] = {"head": "cty/fix-order", "state": "OPEN", "body": "x"}
    fake.failures[("pr", "view")] = [timeout, timeout]
    assert client.pr_body(186) == "x" and len(sleeps) == 2 and all(0 <= s <= 2.0 for s in sleeps)
    fake.failures[("pr", "create")] = [timeout]
    with pytest.raises(NetworkError):
        client.create_pr("cty/other", "main", "T", "B", scope=scope)
    assert sum(command.argv[1:3] == ("pr", "create") for command in fake.commands) == 1


def test_public_repositories_get_no_mirror_issue(gh, scope):
    client, fake, _ = gh
    fake.private = False
    with pytest.raises(PublicRepository):
        client.issue_create(NewIssue("标题", "正文", "<!-- tightrein:sample:0007 -->"), scope=scope)
    assert fake.issues == {}


def test_an_interrupted_issue_create_is_recovered_by_the_marker(gh, scope, interrupt):
    client, fake, _ = gh
    marker = "<!-- tightrein:sample:0007 -->"
    issue = NewIssue("标题", "正文", marker, labels=("tightrein:todo",), parent=3)
    created = client.issue_create(issue, scope=scope)
    assert (created.number, fake.issues[1]["body"]) == (1, f"正文\n\n{marker}\n")
    argv = fake.commands[-1].argv
    assert ("--label", "tightrein:todo") == argv[argv.index("--label"):argv.index("--label") + 2]
    assert "--parent" in argv
    again = NewIssue("标题", "正文", "<!-- tightrein:sample:0008 -->")
    interrupt(idempotency_key(scope, "issue-create", (again.marker,)))
    fake.issues[2] = {"body": f"正文\n\n{again.marker}\n", "state": "open", "labels": []}
    assert client.issue_create(again, scope=scope).number == 2
    assert len(fake.issues) == 2


def test_only_labels_that_exist_in_the_repository_are_removed(gh, scope):
    client, fake, _ = gh
    client.issue_labels(3, ["tightrein:doing"], ["tightrein:todo", "tightrein:gone"], scope=scope)
    assert fake.commands[-1].argv == ("gh", "issue", "edit", "3", "--add-label", "tightrein:doing",
                                      "--remove-label", "tightrein:todo", "--repo", SLUG)
