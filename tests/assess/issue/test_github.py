from types import SimpleNamespace

import pytest

from tightrein.assess.issue import files, github, transitions
from tightrein.assess.issue.transitions import IssueEvent
from tightrein.onboard.setup import ModuleSetup, ModuleStatus, Setup
from tightrein.protocol.git import GitError, PublicRepository
from tightrein.protocol.git.github import IssueRef, RemoteIssue
from tightrein.store.tables import issues

SECRET = "sk-live-123456"


class FakeGitHub:
    slug = "acme/shop"

    def __init__(self) -> None:
        self.created: list = []
        self.calls: list[tuple] = []
        self.existing_labels = {"tightrein:todo"}
        self.fail_create = 0
        self.public = False
        self.remote: dict[int, RemoteIssue] = {}

    def labels(self) -> set[str]:
        return set(self.existing_labels)

    def label_create(self, name: str, color: str, description: str, *, scope) -> str:
        self.existing_labels.add(name)
        self.calls.append(("label_create", name))
        return name

    def issue_create(self, issue, *, scope) -> IssueRef:
        if self.public:
            raise PublicRepository("公开仓库不建镜像")
        if self.fail_create:
            self.fail_create -= 1
            raise GitError("gh: 502")
        self.created.append(issue)
        return IssueRef(len(self.created) + 40, f"https://github.com/acme/shop/issues/{len(self.created) + 40}")

    def issue_labels(self, number: int, add, remove, *, scope) -> int:
        self.calls.append(("labels", number, tuple(add), tuple(remove)))
        return number

    def issue_close(self, number: int, reason: str, *, scope) -> int:
        self.calls.append(("close", number, reason))
        return number

    def issue_reopen(self, number: int, *, scope) -> int:
        self.calls.append(("reopen", number))
        return number

    def issue_states(self) -> dict[int, RemoteIssue]:
        return dict(self.remote)


def _setup(status: ModuleStatus) -> Setup:
    module = ModuleSetup(github.SETUP_KEY, status, None, None, None, None, None)
    return Setup("demo", "2026-10-08T00:00:00Z", {github.SETUP_KEY: module})


@pytest.fixture
def hub(runtime) -> FakeGitHub:
    runtime.setup = _setup(ModuleStatus.ENABLED)
    runtime.github = FakeGitHub()
    runtime.redactor.register(SECRET)
    return runtime.github


@pytest.fixture
def mirrored(runtime, make_issue, hub):
    record = make_issue("0007", status="todo", extra={"triageCommit": "c" * 40})
    body = files.read_body(runtime.workspace, "0007").replace("订单查询没有按用户过滤", f"订单查询没有按用户过滤，令牌 {SECRET}")
    record.extra["history"] = [{"at": "2026-10-08T03:00:00Z", "event": "create", "actor": "tightrein",
                                "reason": None, "note": "内部记录"}]
    files.write(runtime, record, body=body)
    github.sync(runtime, issues.get(runtime.conn, "0007"))
    return issues.get(runtime.conn, "0007")


def test_creating_a_local_issue_creates_the_mirror_with_a_redacted_body(mirrored, hub):
    created = hub.created[0]
    assert SECRET not in created.body  # 整体脱敏
    assert "内部记录" not in created.body and "## 历史" not in created.body  # 去掉历史
    link = f"[`services/orders.py:3`](https://github.com/acme/shop/blob/{'c' * 40}/services/orders.py#L3)"
    assert link in created.body  # 代码位置换成取证 commit 的永久链接
    assert created.marker in created.body and created.marker == "<!-- tightrein:demo:0007 -->"
    assert created.labels == ("tightrein:todo",)
    assert mirrored.extra["github"] == {"number": 41, "url": "https://github.com/acme/shop/issues/41",
                                        "state": "open", "label": "tightrein:todo"}


def test_status_changes_move_the_label_and_close_or_reopen(runtime, mirrored, hub):
    closed = transitions.close(runtime, "0007", "wont_fix")
    assert ("label_create", "tightrein:cancelled") in hub.calls
    assert ("labels", 41, ("tightrein:cancelled",), ("tightrein:todo",)) in hub.calls
    assert ("close", 41, "not planned") in hub.calls
    assert issues.get(runtime.conn, "0007").extra["github"]["state"] == "closed" and closed.status == "cancelled"
    transitions.apply_event(runtime, "0007", IssueEvent.REOPEN)
    assert ("reopen", 41) in hub.calls
    assert len(hub.created) == 1  # 不重复建


def test_public_repositories_are_refused(runtime, make_issue, hub):
    hub.public = True
    record = make_issue("0007", status="todo")
    github.sync(runtime, record)
    mirror = issues.get(runtime.conn, "0007").extra["github"]
    assert "公开仓库" in mirror["skipped"]
    hub.public = False
    github.sync(runtime, issues.get(runtime.conn, "0007"))
    assert hub.created == []  # 整次跳过，之后也不再建


def test_failures_are_recorded_and_retried_without_duplicates(runtime, make_issue, hub):
    hub.fail_create = 1
    github.sync(runtime, make_issue("0007", status="todo"))
    assert issues.get(runtime.conn, "0007").extra["github"]["error"] == "gh: 502"
    assert github.retry_failed(runtime) == 1
    assert len(hub.created) == 1 and "error" not in issues.get(runtime.conn, "0007").extra["github"]
    assert github.retry_failed(runtime) == 0


def test_closing_or_reopening_on_github_is_read_back_as_a_user_action(runtime, mirrored, hub, make_issue):
    hub.remote = {41: RemoteIssue(41, "closed", "NOT_PLANNED")}
    assert github.read_back(runtime) == ["0007"]
    issue = issues.get(runtime.conn, "0007")
    assert issue.status == "cancelled" and issue.extra["closeReason"] == "wont_fix"
    assert issue.extra["history"][-1]["actor"] == "github"
    assert not [call for call in hub.calls if call[0] == "close"]  # 读回引起的转换不再回写
    hub.remote = {41: RemoteIssue(41, "open", "REOPENED")}
    assert github.read_back(runtime) == ["0007"] and issues.get(runtime.conn, "0007").status == "todo"


def test_a_pr_merge_closing_the_issue_is_not_a_user_close(runtime, mirrored, hub):
    for event in (IssueEvent.START, IssueEvent.DELIVER, IssueEvent.MERGE):
        transitions.apply_event(runtime, "0007", event, actor="tightrein")
    hub.remote = {41: RemoteIssue(41, "closed", "COMPLETED")}
    assert github.read_back(runtime) == []
    issue = issues.get(runtime.conn, "0007")
    assert issue.status == "accepting" and issue.extra["github"]["state"] == "closed"  # 只记状态


def test_disabled_mirror_does_nothing(runtime, make_issue):
    runtime.setup = _setup(ModuleStatus.DISABLED)  # 是否启用看接入清单
    runtime.github = SimpleNamespace()
    github.sync(runtime, make_issue("0007", status="todo"))
    assert "github" not in issues.get(runtime.conn, "0007").extra
    assert github.read_back(runtime) == []


def test_the_mirror_follows_the_setup_checklist(runtime, make_issue, hub):
    runtime.setup = _setup(ModuleStatus.DISABLED)
    github.sync(runtime, make_issue("0007", status="todo"))
    assert hub.created == [] and not github.enabled(runtime)
    runtime.setup = Setup("demo", "2026-10-08T00:00:00Z", {})  # 清单里没有这一项也不同步
    assert not github.enabled(runtime)
    runtime.setup = _setup(ModuleStatus.ENABLED)
    assert github.enabled(runtime)
    runtime.github = None  # origin 不是 GitHub 仓库
    assert not github.enabled(runtime)
