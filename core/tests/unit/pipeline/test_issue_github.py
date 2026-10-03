"""GitHub Issue 镜像：建镜像、状态标签、关键节点评论、开关状态、读回、确认模式、失败与重试(architecture/06 第 13 节)。"""

import json

import pytest
from pipeline_world import PROJECT, make_world

from tightrein.domain.enums import CloseReason, IssueEvent, IssueStatus, OperationStatus, Severity, Stage, TaskType
from tightrein.observability.redact import Redactor
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.pipeline.issue.steps import github, transitions
from tightrein.pipeline.issue.steps.github import GithubMirror
from tightrein.store import idempotency
from tightrein.store.files import issue_files
from tightrein.store.repos import github_mirror, issue_events, issues, pending_operations
from tightrein.vcs.executor import OperationRunner
from tightrein.vcs.gh_issues import GhIssues
from tightrein.vcs.operations import OperationPlanner
from tightrein.vcs.process import Completed, VcsProcess

RUN = "R-20261005-030000-issue"
SLUG = "owner/name"
URL = "https://github.com/owner/name/issues/{number}"
SECRET = "Bearer abcdefghijklmnopqrstuvwxyz0123"


class FakeGithub:
    """按内存状态应答的 gh：issues 为 {编号: {state, reason, labels, comments, body}}；fail 中的子命令以退出码 1 结束。"""

    def __init__(self, private=True):
        self.private = private
        self.labels = set()
        self.issues = {}
        self.calls = []
        self.fail = set()

    def _value(self, argv, flag):
        return argv[argv.index(flag) + 1]

    def _optional(self, argv, flag):
        return self._value(argv, flag) if flag in argv else None

    def _read(self, argv, flag):
        return open(self._value(argv, flag), encoding="utf-8").read()

    def __call__(self, command):
        argv = command.argv[1:]
        self.calls.append(argv)
        name = " ".join(argv[:2])
        if name in self.fail:
            return Completed(command.argv, 1, "", "HTTP 502\n")
        if name == "repo view":
            return Completed(command.argv, 0, json.dumps({"isPrivate": self.private, "hasIssuesEnabled": True}))
        if name == "label list":
            return Completed(command.argv, 0, json.dumps([{"name": label} for label in sorted(self.labels)]))
        if name == "label create":
            self.labels.add(argv[2])
            return Completed(command.argv, 0, "")
        if name == "issue list":
            items = [{"number": number, "url": URL.format(number=number), "state": item["state"].upper(),
                      "stateReason": item["reason"], "body": item["body"]} for number, item in self.issues.items()]
            return Completed(command.argv, 0, json.dumps(items))
        if name == "issue create":
            number = len(self.issues) + 1
            labels = {argv[index + 1] for index, part in enumerate(argv) if part == "--label"}
            self.issues[number] = {"state": "open", "reason": "", "labels": labels, "comments": [],
                                   "body": self._read(argv, "--body-file"), "title": self._value(argv, "--title"),
                                   "parent": self._optional(argv, "--parent"),
                                   "blockedBy": self._optional(argv, "--blocked-by")}
            return Completed(command.argv, 0, URL.format(number=number) + "\n")
        item = self.issues[int(argv[2])]
        if name == "issue edit" and "--title" in argv:
            item["title"], item["body"] = self._value(argv, "--title"), self._read(argv, "--body-file")
        elif name == "issue edit" and ("--parent" in argv or "--add-blocked-by" in argv):
            item["parent"] = self._optional(argv, "--parent") or item.get("parent")
            item["blockedBy"] = self._optional(argv, "--add-blocked-by") or item.get("blockedBy")
        elif name == "issue edit":
            item["labels"] |= set((self._optional(argv, "--add-label") or "").split(",")) - {""}
            item["labels"] -= set((self._optional(argv, "--remove-label") or "").split(","))
        elif name == "issue comment":
            item["comments"].append(self._read(argv, "--body-file"))
        elif name == "issue close":
            item["state"], item["reason"] = "closed", self._value(argv, "--reason").upper().replace(" ", "_")
        elif name == "issue reopen":
            item["state"], item["reason"] = "open", "REOPENED"
        return Completed(command.argv, 0, "")

    def writes(self):
        return [call[:2] for call in self.calls if call[:2] not in (("repo", "view"), ("label", "list"),
                                                                       ("issue", "list"))]


class Mirrored:
    def __init__(self, tmp_path, confirm=False, gh=None, tracker="github"):
        repo = tmp_path / "repo"
        repo.mkdir()
        self.world = make_world(tmp_path, project={**PROJECT["project"], "repo": str(repo)},
                                issues={"tracker": tracker}, gates={"mirror-writes": "user" if confirm else "auto"})
        self.gh = gh or FakeGithub()
        world = self.world
        self.process = VcsProcess(execute=self.gh, environ={}, sleep=lambda seconds: None)
        self.env = transitions.IssueEnv(world.conn, world.layout, world.clock, world.config)
        self.planner = OperationPlanner(world.conn, None, None, world.layout, world.config, world.clock, RUN)
        remotes = {"remote.origin.url": ("git@github.com:owner/name.git",)}
        self.mirror = GithubMirror(self.env, self.process, GhIssues(self.process, repo, 100), lambda path: remotes,
                                   Redactor(), RUN, lambda: self.planner)
        self.service = IssueService(IssueDeps(world.layout, world.config, world.conn, world.clock, world.events,
                                              mirror=self.mirror))

    def create(self, text="订单列表增加按日期筛选。"):
        return self.service.create_manual("按日期筛选订单", text, Severity.P1)

    def record(self, issue_id="0001"):
        return issues.get(self.world.conn, issue_id)

    def confirm_all(self):
        runner = OperationRunner(self.world.conn, self.process, None, None, self.world.layout, RUN,
                                 follow_ups={Stage.ISSUE: self.mirror.follow_up()})
        done = []
        for record in pending_operations.find(self.world.conn, status=OperationStatus.PENDING):
            runner.confirm(record.id, confirmed_by="cty", clock=self.world.clock)
            done.append(runner.execute(record.id, clock=self.world.clock))
        return done


def test_local_tracker_never_calls_gh(tmp_path):
    def refuse(command):
        raise AssertionError(command.argv)

    mirrored = Mirrored(tmp_path, gh=refuse, tracker="local")
    mirrored.create()
    mirrored.service.close("0001", CloseReason.WONT_FIX)
    assert mirrored.service.sync().github.repo is None
    assert github_mirror.unposted(mirrored.world.conn) == []


def test_creating_a_local_issue_creates_the_mirror_with_a_redacted_body(tmp_path):
    mirrored = Mirrored(tmp_path)
    record = mirrored.create(f"订单列表增加按日期筛选。调用时带 {SECRET}")
    remote = mirrored.gh.issues[1]
    assert remote["title"] == "按日期筛选订单" and remote["labels"] == set()
    assert remote["body"].startswith("## 问题\n\n订单列表增加按日期筛选")
    assert "## 历史" not in remote["body"]
    assert "abcdefghijklmnop" not in remote["body"] and "<!-- tightrein:demo:0001 -->" in remote["body"]
    assert len(mirrored.gh.labels) == len(github.STANDARD_TYPE_LABELS)
    current = mirrored.record()
    assert current.issue.github.number == 1 and current.issue.github.url == URL.format(number=1)
    document = issue_files.read(mirrored.world.layout.root / record.path)
    assert document.issue.github == current.issue.github and "已建 GitHub Issue owner/name#1" in document.body
    assert github.unsynced(mirrored.world.conn, mirrored.world.config) == []


def test_status_labels_comments_and_close_follow_the_local_issue(tmp_path):
    mirrored = Mirrored(tmp_path)
    mirrored.create()
    mirrored.service.approve("0001")
    mirrored.service.close("0001", CloseReason.WONT_FIX, note="不做了")
    remote = mirrored.gh.issues[1]
    assert remote["labels"] == set() and (remote["state"], remote["reason"]) == ("closed", "NOT_PLANNED")
    assert remote["comments"][0].startswith("本地 Issue 已关闭，原因：不修。不做了")
    calls = mirrored.gh.writes()
    assert calls.index(("issue", "comment"), 1) < calls.index(("issue", "close"))
    mirrored.service.reopen("0001")
    assert remote["state"] == "open" and remote["labels"] == set()
    before = len(mirrored.gh.writes())
    mirrored.service.sync()
    assert len(mirrored.gh.writes()) == before


def test_closing_or_reopening_on_github_is_read_back_as_a_user_action(tmp_path):
    mirrored = Mirrored(tmp_path)
    mirrored.create()
    mirrored.gh.issues[1].update(state="closed", reason="NOT_PLANNED")
    report = mirrored.service.sync().github
    issue = mirrored.record().issue
    assert report.closed == ["0001"] and (issue.status, issue.close_reason) == (IssueStatus.CANCELLED,
                                                                               CloseReason.WONT_FIX)
    event = issue_events.for_issue(mirrored.world.conn, "0001")[-1]
    assert (event.event, event.actor) == ("user-closed", "user") and "在 GitHub 上关闭 #1" in event.note
    assert mirrored.gh.issues[1]["comments"] == [] and mirrored.gh.issues[1]["labels"] == set()
    mirrored.gh.issues[1].update(state="open", reason="REOPENED")
    assert mirrored.service.sync().github.reopened == ["0001"]
    assert mirrored.record().issue.status is IssueStatus.TODO
    assert mirrored.gh.issues[1]["labels"] == set()


def _to_pr_review(mirrored):
    env = mirrored.env
    for event in (IssueEvent.FIX_STARTED, IssueEvent.FIX_DONE, IssueEvent.VERIFY_PASSED, IssueEvent.PR_CREATED):
        transitions.apply_event(env, transitions.record_of(env, "0001"), event, actor="release", note="PR #3")


def test_a_pr_merge_closing_the_issue_is_not_a_user_close(tmp_path):
    mirrored = Mirrored(tmp_path)
    mirrored.create()
    _to_pr_review(mirrored)
    mirrored.service.sync()
    assert mirrored.gh.issues[1]["comments"][-1].startswith("PR 已创建。PR #3")
    mirrored.gh.issues[1].update(state="closed", reason="COMPLETED")
    report = mirrored.service.sync().github
    assert report.closed == [] and mirrored.record().issue.status is IssueStatus.PENDING_MERGE
    assert github_mirror.get(mirrored.world.conn, "0001").remote_state == "closed"
    assert ("issue", "reopen") not in mirrored.gh.writes()


def test_public_repositories_are_refused(tmp_path):
    mirrored = Mirrored(tmp_path, gh=FakeGithub(private=False))
    mirrored.create()
    report = mirrored.service.sync().github
    assert "公开仓库" in report.skipped and mirrored.gh.writes() == []
    assert report.unsynced == ["0001：未建 GitHub Issue"]


def test_failures_are_recorded_and_retried_without_duplicates(tmp_path):
    mirrored = Mirrored(tmp_path)
    mirrored.gh.fail.add("issue create")
    mirrored.create()
    state = github_mirror.get(mirrored.world.conn, "0001")
    assert "HTTP 502" in state.error and mirrored.record().issue.github is None
    assert github.unsynced(mirrored.world.conn, mirrored.world.config)[0].startswith("0001：未建 GitHub Issue；最近一次失败")
    mirrored.gh.fail.clear()
    mirrored.service.sync()
    assert list(mirrored.gh.issues) == [1] and github_mirror.get(mirrored.world.conn, "0001").error is None


def test_an_interrupted_create_is_recovered_by_the_marker(tmp_path):
    mirrored = Mirrored(tmp_path)
    mirrored.gh.fail.add("repo view")
    mirrored.create()
    mirrored.gh.fail.clear()
    mirrored.gh.issues[7] = {"state": "open", "reason": "", "labels": set(), "comments": [],
                             "body": "x\n<!-- tightrein:demo:0001 -->\n"}
    idempotency.begin(mirrored.world.conn, "github-issue:owner/name:0001", mirrored.world.clock)
    mirrored.service.sync()
    assert ("issue", "create") not in mirrored.gh.writes() and mirrored.record().issue.github.number == 7


def test_confirm_mode_writes_only_after_confirmation(tmp_path):
    mirrored = Mirrored(tmp_path, confirm=True)
    mirrored.create()
    assert mirrored.gh.writes() == [] and mirrored.record().issue.github is None
    unsynced = github.unsynced(mirrored.world.conn, mirrored.world.config)
    assert unsynced == ["0001：未建 GitHub Issue(等待确认 OP-0001)"]
    mirrored.service.sync()
    assert len(mirrored.confirm_all()) == 1
    assert mirrored.record().issue.github.number == 1 and mirrored.gh.issues[1]["labels"] == set()
    mirrored.service.close("0001", CloseReason.NOT_A_BUG)
    [result] = mirrored.confirm_all()
    assert result.status is OperationStatus.EXECUTED
    assert mirrored.gh.issues[1]["state"] == "closed" and len(mirrored.gh.issues[1]["comments"]) == 1
    assert github.unsynced(mirrored.world.conn, mirrored.world.config) == []


def test_confirm_mode_does_not_repeat_a_rejected_operation(tmp_path):
    mirrored = Mirrored(tmp_path, confirm=True)
    mirrored.create()
    runner = OperationRunner(mirrored.world.conn, mirrored.process, None, None, mirrored.world.layout, RUN)
    runner.reject("OP-0001", note="先不同步", clock=mirrored.world.clock)
    assert mirrored.service.sync().github.operations == []


@pytest.mark.parametrize("remote, slug", [("git@github.com:owner/name.git", SLUG), ("/srv/git/name.git", None)])
def test_the_repository_comes_from_origin(tmp_path, remote, slug):
    mirrored = Mirrored(tmp_path)
    mirrored.mirror.remotes = lambda path: {"remote.origin.url": (remote,)}
    if slug is None:
        with pytest.raises(ValueError, match="issues.github.repo"):
            mirrored.mirror.slug()
    else:
        assert mirrored.mirror.slug() == slug


def test_the_mirror_body_links_code_to_the_evidence_commit_and_folds_the_evidence(tmp_path):
    from dataclasses import replace

    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True)
    (repo / "utils" / "log.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "w").mkdir()
    mirrored = Mirrored(tmp_path / "w")
    issue = replace(mirrored.create().issue, triage_commit="c" * 40)
    text = ("# t\n\n## 问题\n\n见 `utils/log.py:127-129` 与 utils/log.py:3，`gone.py:1` 不链接\n\n"
            "## 完整证据\n\n- `utils/log.py:127` 一\n- 二\n\n```\nutils/log.py:9\n```\n\n## 历史\n\n- h\n")
    found = github.mirror_body(issue, text, "demo", SLUG, repo, "zh", Redactor())
    link = "https://github.com/owner/name/blob/" + "c" * 40 + "/utils/log.py#L127-L129"
    assert f"[`utils/log.py:127-129`]({link})" in found and "#L3-L3)" in found and "`gone.py:1` 不链接" in found
    assert "<details>\n<summary>展开 2 条证据</summary>" in found and "```\nutils/log.py:9\n```" in found
    assert "## 历史" not in found and not found.startswith("# t")
    assert "](" not in github.mirror_body(replace(issue, triage_commit=None), text, "demo", SLUG, repo, "zh",
                                          Redactor())


def test_refresh_updates_the_title_and_body_of_an_existing_mirror(tmp_path):
    mirrored = Mirrored(tmp_path)
    mirrored.create()
    mirrored.mirror.refresh(["0001"])
    remote = mirrored.gh.issues[1]
    assert ("issue", "edit", "1", "--repo", SLUG, "--title", "按日期筛选订单") == tuple(mirrored.gh.calls[-1][:7])
    assert remote["body"].startswith("## 问题") and len(mirrored.gh.issues) == 1
    mirrored.mirror.sync(["0001"])
    assert not any("--title" in call for call in mirrored.gh.calls[-3:])


def test_type_labels_sub_issues_and_blocking_follow_the_local_issue(tmp_path):
    from dataclasses import replace

    from tightrein.store.files.issue_files import IssueDocument

    mirrored = Mirrored(tmp_path)
    parent = mirrored.service.create_manual("拆分的父任务", "整体需求。", Severity.P1, TaskType.FEATURE)
    first = mirrored.service.create_manual("第一个子任务", "一。", Severity.P1, TaskType.FEATURE)
    assert mirrored.gh.issues[1]["labels"] == {"enhancement"}
    path = mirrored.world.layout.root / first.path
    document = issue_files.read(path)
    issue_files.write(mirrored.world.conn, mirrored.world.layout,
                      IssueDocument(replace(document.issue, parent=parent.issue.id), document.body))
    mirrored.service.sync()
    assert mirrored.gh.issues[2]["parent"] == "1"
    second = create_child(mirrored, parent.issue.id, first.issue.id)
    remote = mirrored.gh.issues[second.issue.github.number]
    assert (remote["parent"], remote["blockedBy"]) == ("1", "2")
    calls = len(mirrored.gh.writes())
    mirrored.service.sync()
    assert len(mirrored.gh.writes()) == calls and github.unsynced(mirrored.world.conn, mirrored.world.config) == []


def create_child(mirrored, parent_id, previous):
    from tightrein.pipeline.issue.steps import create

    world = mirrored.world
    record = create.create_manual(world.conn, world.layout, world.clock, title="第二个子任务", requirement="二。",
                                  severity=Severity.P1, slug_max_length=40, language="zh", depends_on=previous,
                                  task_type=TaskType.FEATURE, parent=parent_id)
    mirrored.service.mirror([record.issue.id])
    return mirrored.record(record.issue.id)


def test_labels_from_the_eight_status_version_are_replaced(tmp_path):
    from dataclasses import replace

    mirrored = Mirrored(tmp_path)
    mirrored.create()
    state = github_mirror.get(mirrored.world.conn, "0001")
    github_mirror.save(mirrored.world.conn, replace(state, labelled_status="in-review+needs-decision"))
    mirrored.gh.issues[1]["labels"] = {"tightrein:in-review", "tightrein:needs-decision"}
    mirrored.service.sync()
    assert mirrored.gh.issues[1]["labels"] == set()


def test_humanized_mirror_body_for_handoff_triage_issue(tmp_path):
    from dataclasses import replace

    repo = tmp_path / "repo"
    (repo / "processors").mkdir(parents=True)
    (repo / "processors" / "review.py").write_text("code\n", encoding="utf-8")
    (tmp_path / "w").mkdir()
    mirrored = Mirrored(tmp_path / "w")
    issue = replace(mirrored.create().issue, triage_commit="a" * 40)

    text = (
        "## 结论\n\n[模块] 崩溃问题；后果：无法运行\n\n"
        "## 内容\n\n"
        "### 问题\n\n读取损坏文件时崩溃。\n\n- 期望：正常跳过\n- 实际：抛出异常崩溃\n\n"
        "### 影响\n\n- 严重度：P2(理由)\n- 后果：无法运行\n- 范围(调用点)：`main.py:1`\n\n"
        "### 复现\n\n1. 构造损坏文件\n2. 运行 review\n\n"
        "### 原因\n\n- 触发条件：进程被强杀留下半截文件\n- 根因位置：`processors/review.py:10`\n- 引入：commit1(作者 A，PR —)\n\n"
        "### 范围\n\n- 可能涉及的文件：`processors/review.py`\n- 不在本次范围内：其他模块\n\n"
        "### 注意事项\n\n- 不能改：接口签名\n\n"
        "### 验收标准\n\n- [ ] 复现测试修复前失败修复后通过\n- [ ] 现有测试全部通过\n\n"
        "### 修复方向\n\n添加 try/except 并在写入时使用原子写入。\n\n"
        "## 需要决定\n\n无\n\n"
        "## 下一步\n\n- [ ] 审阅并放行：tightrein issue approve 1(user)\n\n"
        "## 引用\n\n- `data/findings/P-001.md` 报告\n- `processors/review.py:10` 没有容错\n\n"
        "## 历史\n\n- 2026-10-04 创建\n"
    )
    found = github.mirror_body(issue, text, "demo", SLUG, repo, "zh", Redactor())

    # 验证人类化的小节结构（原因、问题、方法）
    assert "## 原因" in found
    assert "- 触发条件：进程被强杀留下半截文件" in found
    assert f"[`processors/review.py:10`](https://github.com/owner/name/blob/{'a' * 40}/processors/review.py#L10-L10)" in found
    assert "## 问题" in found
    assert "读取损坏文件时崩溃。" in found
    assert "### 复现步骤" in found
    assert "1. 构造损坏文件" in found
    assert "## 方法" in found
    assert "添加 try/except 并在写入时使用原子写入。" in found
    assert "涉及文件" in found and "processors/review.py" in found

    # 验证去除了机器和 AI 流水线生成的浓重痕迹
    assert "## 结论" not in found
    assert "## 内容" not in found
    assert "## 需要决定" not in found
    assert "## 下一步" not in found
    assert "tightrein issue approve" not in found
    assert "### 注意事项" not in found
    assert "不能改：接口签名" not in found
    assert "### 验收标准" not in found
    assert "复现测试修复前失败" not in found
    assert "这是 tightrein 本地 Issue" not in found
    assert "## 历史" not in found
    assert "<!-- tightrein:demo:0001 -->" in found

