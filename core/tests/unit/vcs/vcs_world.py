"""vcs 写操作测试共用的环境：真实的 git 仓库(origin 为本地裸仓库)、按内存状态应答的假 gh、临时数据库。"""

import json
from datetime import datetime, timezone

from tightrein.domain.clock import FixedClock
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.migrations.runner import open_database
from tightrein.vcs.executor import OperationRunner
from tightrein.vcs.gh_read import GhReader
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.operations import OperationPlanner
from tightrein.vcs.process import Completed, VcsProcess, subprocess_executor

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
RUN = "R-20261005-030000-release"
PR_URL = "https://github.com/cty/sample/pull/{number}"
ISSUE_URL = "https://github.com/cty/sample/issues/{number}"


class FakeGh:
    """gh 的替身：pulls 为 {编号: {"head", "state", "body"}}；pr create 新增一个打开的 PR 并输出链接；issue create
    记下正文并输出链接，pr merge 把 PR 改为已合并，其余 issue 命令成功且没有输出。"""

    def __init__(self):
        self.pulls = {}
        self.issues = []
        self.calls = []

    def _value(self, argv, flag):
        return argv[argv.index(flag) + 1]

    def __call__(self, command):
        argv = command.argv
        self.calls.append(argv)
        if argv[1:3] == ("pr", "list"):
            head = self._value(argv, "--head")
            items = [{"number": number, "url": PR_URL.format(number=number), "state": pull["state"]}
                     for number, pull in self.pulls.items() if pull["head"] == head]
            return Completed(argv, 0, json.dumps(items))
        if argv[1:3] == ("pr", "view"):
            number = int(argv[3])
            pull = self.pulls[number]
            data = {"body": pull["body"], "number": number, "url": PR_URL.format(number=number),
                    "state": pull["state"]}
            return Completed(argv, 0, json.dumps(data))
        if argv[1:3] == ("pr", "create"):
            number = max(self.pulls, default=185) + 1
            body = open(self._value(argv, "--body-file"), encoding="utf-8").read()
            self.pulls[number] = {"head": self._value(argv, "--head"), "state": "OPEN", "body": body}
            return Completed(argv, 0, PR_URL.format(number=number) + "\n")
        if argv[1:3] == ("pr", "edit"):
            number = int(argv[3])
            self.pulls[number]["body"] = open(self._value(argv, "--body-file"), encoding="utf-8").read()
            return Completed(argv, 0, PR_URL.format(number=number) + "\n")
        if argv[1:3] == ("pr", "comment"):
            self.pulls[int(argv[3])].setdefault("comments", []).append(
                open(self._value(argv, "--body-file"), encoding="utf-8").read())
            return Completed(argv, 0, PR_URL.format(number=int(argv[3])) + "#issuecomment-1\n")
        if argv[1:3] == ("pr", "merge"):
            self.pulls[int(argv[3])]["state"] = "MERGED"
            return Completed(argv, 0, "")
        if argv[1:3] == ("issue", "create"):
            self.issues.append(open(self._value(argv, "--body-file"), encoding="utf-8").read())
            return Completed(argv, 0, ISSUE_URL.format(number=len(self.issues)) + "\n")
        if argv[1] == "issue":
            return Completed(argv, 0, "")
        raise AssertionError(f"没有模拟的 gh 命令：{argv}")


class VcsWorld:
    def __init__(self, repos, make_config, tmp_path):
        self.repos = repos
        self.clock = FixedClock(NOW)
        self.origin, self.repo = repos.origin_and_clone()
        self.layout = WorkspaceLayout(tmp_path / "ws")
        self.config = make_config(
            project={"name": "sample", "repo": str(self.repo), "mainBranch": "main"},
            git={"worktreeLinks": [".claude/"]},
        )
        repos.write(self.repo, ".claude/settings.json", "{}\n")
        self.gh = FakeGh()

        def execute(command):
            return self.gh(command) if command.argv[0] == "gh" else subprocess_executor(command)

        self.process = VcsProcess(execute=execute, environ=repos.environ, sleep=lambda seconds: None)
        self.git = GitReader(self.process)
        self.gh_reader = GhReader(self.process)
        self.conn = open_database(self.layout.database(), self.clock)
        self.planner = OperationPlanner(self.conn, self.git, self.gh_reader, self.layout, self.config, self.clock, RUN)
        self.runner = OperationRunner(self.conn, self.process, self.git, self.gh_reader, self.layout, RUN)

    def fix_worktree(self, issue="0007", branch="cty/fix-order"):
        worktree = self.layout.fix_worktree(issue)
        self.repos.git(self.repo, "worktree", "add", "-q", "-b", branch, str(worktree), "origin/main")
        return worktree

    def confirm_and_execute(self, operation):
        confirmed = self.runner.confirm(operation.id, confirmed_by="cty", clock=self.clock)
        while confirmed.confirmations_given < confirmed.confirmations_required:
            confirmed = self.runner.confirm(operation.id, confirmed_by="cty", clock=self.clock)
        return self.runner.execute(operation.id, clock=self.clock)

    def upstream_commit(self, files):
        other = self.repos.root / "other"
        if not other.exists():
            self.repos.git(self.repos.root, "clone", "-q", str(self.origin), str(other))
        self.repos.git(other, "pull", "-q", "origin", "main")
        commit = self.repos.commit(other, "feat: upstream", files)
        self.repos.git(other, "push", "-q", "origin", "main")
        return commit
