"""GitHub 上已有的 Issue(collect.github_issues 导入的问题)：需求直接写成待修并接手原编号，缺陷报告取证后接手原编号；
重复、不成立、不修、主干上已修的在 GitHub 上留一句话关掉，写的话不留工具痕迹。模型调用是录制的输出。"""

import pytest

from tightrein.assess import assess as assessing
from tightrein.assess.issue import files
from tightrein.onboard.setup import ModuleSetup, ModuleStatus, Setup
from tightrein.protocol.git import GitError
from tightrein.protocol.git.github import IssueRef
from tightrein.store.tables import issues, problems

TRIAGE = "assess.triage"
SOURCE = "collect.github_issues"
URL = "https://github.com/acme/shop/issues/{number}"


class FakeHub:
    slug = "acme/shop"

    def __init__(self) -> None:
        self.created: list = []
        self.calls: list[tuple] = []
        self.fail_comment = False

    def labels(self) -> set[str]:
        return {"bug", "enhancement"}

    def pr_for_commit(self, commit: str):  # 归因查引入的 PR
        return None

    def issue_create(self, issue, *, scope) -> IssueRef:
        self.created.append(issue)
        return IssueRef(90, URL.format(number=90))

    def issue_comment(self, number: int, body: str, *, scope) -> str:
        if self.fail_comment:
            raise GitError("gh: 502")
        self.calls.append(("comment", number, body))
        return f"{URL.format(number=number)}#c"

    def issue_close(self, number: int, reason: str, *, scope) -> int:
        self.calls.append(("close", number, reason))
        return number

    def issue_reopen(self, number: int, *, scope) -> int:
        self.calls.append(("reopen", number))
        return number

    def comments(self) -> list[str]:
        return [call[2] for call in self.calls if call[0] == "comment"]


@pytest.fixture
def hub(runtime) -> FakeHub:
    module = ModuleSetup("release.github_issues", ModuleStatus.ENABLED, None, None, None, None, None)
    runtime.setup = Setup("demo", "2026-10-08T00:00:00Z", {"release.github_issues": module})
    runtime.github = FakeHub()
    return runtime.github


@pytest.fixture
def imported(make_problem):
    def make(number: int = 12, *, kind: str = "report", title: str = "保存订单时报错",
             location: str | None = "services/orders.py:3", body: str = "点保存就报 500", problem: str = "P-0001"):
        return make_problem(problem, source=SOURCE, check_type=kind, title=title, location=location,
                            flags={"deterministic": True},
                            evidence={"number": number, "url": URL.format(number=number), "labels": [], "body": body})

    return make


def assert_human(texts):
    for text in texts:
        assert "tightrein" not in text.lower() and "P-0" not in text and "0001" not in text and "自动" not in text


def test_a_feature_request_becomes_a_todo_user_issue_that_adopts_the_github_number(runtime, hub, imported, agent,
                                                                                   readonly, layout):
    imported(15, kind="request", title="订单列表加个导出按钮", location=None,
             body="希望能导出订单。\n\n## 验收标准\n- [ ] 列表页有导出按钮")
    outcome = assessing.assess(runtime, "P-0001")
    assert agent.calls == []  # 不取证
    assert (outcome.destination, outcome.issue, outcome.verdict, outcome.disposition) == (
        "issue", "0001", "confirmed", "fix_later")
    issue = issues.get(runtime.conn, "0001")
    assert (issue.status, issue.origin, issue.kind, issue.title) == ("todo", "user", "feature", "订单列表加个导出按钮")
    assert issue.extra["problems"] == ["P-0001"] and issue.extra["approvedAt"]
    assert issue.extra["github"] == {"number": 15, "url": URL.format(number=15), "state": "open", "label": None}
    text = files.read_body(layout, "0001")
    assert "希望能导出订单" in text and "- [ ] 列表页有导出按钮" in text
    assert hub.created == [] and hub.calls == []  # 不另建，也不评论
    problem = problems.get(runtime.conn, "P-0001")
    assert problem.status == "ongoing" and problem.issue == "0001" and problem.extra["assess"]["request"]
    assert (layout.subject_dir("0001") / "22-assess.issue-handoff.json").is_file()
    assert (layout.subject_dir("P-0001") / "21-assess.triage-handoff.json").is_file()


def test_a_confirmed_report_becomes_an_issue_that_adopts_the_github_number(runtime, hub, imported, agent, readonly,
                                                                          outputs):
    imported(12)
    agent.queue(TRIAGE, outputs.confirmed())
    outcome = assessing.assess(runtime, "P-0001")
    assert agent.points() == [TRIAGE] and outcome.issue == "0001"
    issue = issues.get(runtime.conn, "0001")
    assert issue.origin == "problem" and issue.extra["github"]["number"] == 12
    assert hub.created == [] and hub.calls == []  # 等修复的 PR 用 Closes #12 关掉


def test_a_refuted_report_is_answered_and_closed_as_not_planned(runtime, hub, imported, agent, readonly, outputs):
    imported(12)
    agent.queue(TRIAGE, outputs.refuted())
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.destination == "false_positive"
    assert hub.calls == [("comment", 12, "看了下代码，这里没有问题：路由只对本人开放。"), ("close", 12, "not planned")]
    assert problems.get(runtime.conn, "P-0001").extra["assess"]["github"]["closed"] == "not planned"
    assert_human(hub.comments())


def test_replies_never_carry_internal_wording_and_fixed_ones_say_so(runtime, hub, imported, agent, readonly, outputs):
    imported(12)
    jargon = outputs.refuted(sourceOfPhenomenon={"location": "routes/orders.py:4", "factRef": None,
                                                 "explanation": "主张基于旧版本，当时路由还没有校验。"})
    agent.queue(TRIAGE, jargon)
    assessing.assess(runtime, "P-0001")
    assert hub.calls[0] == ("comment", 12, "看了下代码，这里没有问题。")  # 带「主张」这种内部用语的理由不用
    assert_human(hub.comments())


def test_a_duplicate_is_closed_with_a_reference_to_the_github_number(runtime, hub, imported, make_problem, make_issue,
                                                                     agent, readonly):
    make_problem("P-0002", status="ongoing", issue="0007", title="保存订单时报错")
    make_issue("0007", status="todo", problems_=["P-0002"], title="保存订单时报错",
               extra={"github": {"number": 41, "url": URL.format(number=41), "state": "open", "label": None}})
    imported(12)
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.destination == "merged" and agent.calls == []
    assert hub.calls == [("comment", 12, "和 #41 是同一个问题，在那边一起处理。"), ("close", 12, "not planned")]
    assert_human(hub.comments())


def test_a_duplicate_without_a_github_number_does_not_mention_local_ids(runtime, hub, imported, make_problem,
                                                                        make_issue, agent, readonly):
    make_problem("P-0002", status="ongoing", issue="0007", title="保存订单时报错")
    make_issue("0007", status="todo", problems_=["P-0002"], title="保存订单时报错", extra={"github": {"skipped": "x"}})
    imported(12)
    assessing.assess(runtime, "P-0001")
    assert hub.comments() == ["和已有的一个问题是同一个，在那边一起处理。"]
    assert "0007" not in hub.comments()[0]


def test_a_report_appended_to_an_existing_issue_is_closed_as_a_duplicate(runtime, hub, imported, make_issue, agent,
                                                                         readonly, outputs):
    make_issue("0007", status="todo", extra={"rootKey": ["services/orders.py#OrderService.get"],
                                             "github": {"number": 41, "url": URL.format(number=41), "state": "open",
                                                        "label": None}})
    imported(12)
    agent.queue(TRIAGE, outputs.confirmed())
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.issues == ["0007"]
    assert hub.calls == [("comment", 12, "和 #41 是同一个问题，在那边一起处理。"), ("close", 12, "not planned")]


def test_fixed_on_main_is_closed_as_completed(runtime, hub, imported, agent, readonly, outputs):
    imported(12)
    agent.queue(TRIAGE, outputs.confirmed(fixedOnMain={"commit": "d" * 40, "basis": "该提交加了用户条件"}))
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.destination == "awaiting_deploy"
    assert hub.calls == [("comment", 12, "已经在 ddddddd 修好了。"), ("close", 12, "completed")]


def test_wont_fix_is_answered_with_one_reason(runtime, hub, imported, agent, readonly, outputs):
    imported(12)
    output = outputs.confirmed()
    output["assessment"].update(worth="wont", worthReason="只有内部工具会走到这里，改要动三个模块。以后再看")
    agent.queue(TRIAGE, output)
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.destination == "wont_fix"
    assert hub.calls == [("comment", 12, "这个先不改：只有内部工具会走到这里，改要动三个模块。"),
                         ("close", 12, "not planned")]


def test_insufficient_evidence_leaves_the_github_issue_open(runtime, hub, imported, agent, readonly, outputs):
    imported(12)
    agent.queue(TRIAGE, outputs.insufficient())
    assert assessing.assess(runtime, "P-0001").destination == "watch_evidence"
    assert hub.calls == []


def test_github_failures_do_not_fail_the_assessment(runtime, hub, imported, agent, readonly, outputs, layout):
    hub.fail_comment = True
    imported(12)
    agent.queue(TRIAGE, outputs.refuted())
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.status.value == "passed" and outcome.destination == "false_positive"
    problem = problems.get(runtime.conn, "P-0001")
    assert problem.status == "closed" and problem.extra["assess"]["github"] == {"number": 12, "error": "gh: 502"}
    assert ("close", 12, "not planned") not in hub.calls


@pytest.mark.parametrize("language, expected", [
    ("en", "Looked into the code; this isn't a bug: The route is only open to the owner."),
    ("ja", "コードを確認しましたが、不具合ではありませんでした：ルートは本人にしか開いていない。"),
])
def test_replies_follow_the_project_language(runtime, hub, imported, agent, readonly, outputs, kit, language,
                                              expected):
    runtime.settings = kit.make_settings(language=language)
    runtime.language = language
    imported(12)
    reason = "The route is only open to the owner. Other paths check it too" if language == "en" \
        else "ルートは本人にしか開いていない"
    agent.queue(TRIAGE, outputs.refuted(sourceOfPhenomenon={"location": "routes/orders.py:4", "factRef": None,
                                                             "explanation": reason}))
    assessing.assess(runtime, "P-0001")
    assert hub.comments() == [expected]
