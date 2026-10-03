"""用户需求的 Issue：不关联问题、免审阅、正文以「需求」为准，以及各环节在没有关联问题时的处理。"""

import pytest
from triage_world import make_triage_world

from tightrein.contracts.validate import validate
from tightrein.domain.enums import CloseReason, IssueOrigin, IssueStatus, Severity
from tightrein.domain.next_step import next_step
from tightrein.pipeline.issue.render import labels
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.pipeline.issue.steps.edit import problems_of
from tightrein.pipeline.issue.steps.transitions import IssueCommandRejected
from tightrein.store.files import issue_files
from tightrein.store.repos import issue_events, issues, runs

REQUIREMENT = "订单列表增加按日期筛选。\n\n## 背景\n\n客服每天要查前一天的订单。\n"


def service(world):
    return IssueService(IssueDeps(world.layout, world.config, world.conn, world.clock, world.events))


def test_manual_issue_is_todo_without_problems_run_or_review(tmp_path):
    world = make_triage_world(tmp_path)
    record = service(world).create_manual("按日期筛选订单", REQUIREMENT, Severity.P2)
    issue = record.issue
    assert (issue.id, issue.status, issue.origin, issue.problems, issue.findings) == (
        "0001", IssueStatus.TODO, IssueOrigin.MANUAL, (), None)
    assert issue.slug == "manual-1"
    document = issue_files.read(world.layout.root / record.path)
    assert document.issue.origin is IssueOrigin.MANUAL and document.run_id is None
    body = document.body
    assert body.index("### 问题") < body.index("客服每天") < body.index("### 验收标准") < body.index("## 历史")
    assert "**背景**" in body and (issue.source, issue.parent) == ("用户需求", None)
    assert labels.text("manualAcceptance", "zh") in body and "创建(用户需求，免审阅)" in body
    assert issues.get(world.conn, "0001").issue.origin is IssueOrigin.MANUAL
    assert runs.find(world.conn) == []


def test_manual_issue_keeps_the_users_own_acceptance_section(tmp_path):
    world = make_triage_world(tmp_path)
    text = "导出 CSV。\n\n## 验收标准\n\n- 点击导出得到 CSV\n"
    record = service(world).create_manual("Export CSV", text, Severity.P1)
    body = (world.layout.root / record.path).read_text(encoding="utf-8")
    assert body.count("验收标准") == 1 and labels.text("manualAcceptance", "zh") not in body
    assert "- [ ] 点击导出得到 CSV" in body
    assert record.issue.slug == "export-csv" and record.issue.severity is Severity.P1


def test_manual_issue_rejects_empty_text(tmp_path):
    world = make_triage_world(tmp_path)
    with pytest.raises(IssueCommandRejected, match="非空"):
        service(world).create_manual("标题", "  \n", Severity.P2)


def test_approve_on_a_manual_todo_issue_keeps_the_state(tmp_path):
    world = make_triage_world(tmp_path)
    service(world).create_manual("按日期筛选订单", REQUIREMENT, Severity.P2)
    record = service(world).approve("0001")
    assert record.issue.status is IssueStatus.TODO
    assert issue_events.for_issue(world.conn, "0001") == []
    assert next_step(record.issue).command == "issue approve"


def test_manual_issue_closes_and_reopens_without_problems(tmp_path):
    world = make_triage_world(tmp_path)
    service(world).create_manual("按日期筛选订单", REQUIREMENT, Severity.P2)
    record = service(world).close("0001", CloseReason.NOT_A_BUG, note="不做了")
    assert record.issue.close_reason is CloseReason.NOT_A_BUG
    assert service(world).reopen("0001").issue.status is IssueStatus.TODO
    assert service(world).sync().invalid is None


def test_reindex_keeps_the_origin_and_edit_requires_the_manual_sections(tmp_path):
    world = make_triage_world(tmp_path)
    record = service(world).create_manual("按日期筛选订单", REQUIREMENT, Severity.P2)
    service(world).reindex()
    assert issues.get(world.conn, "0001").issue.origin is IssueOrigin.MANUAL
    path = world.layout.root / record.path
    before = issue_files.read(path)
    _, errors = problems_of(path, before)
    assert errors == []
    path.write_text(path.read_text(encoding="utf-8").replace("### 问题", "### 要求"), encoding="utf-8")
    _, errors = problems_of(path, before)
    assert "缺少章节「问题」" in errors


def test_frontmatter_requires_problems_only_for_triage_issues(tmp_path):
    world = make_triage_world(tmp_path)
    record = service(world).create_manual("按日期筛选订单", REQUIREMENT, Severity.P2)
    data = issue_files.to_frontmatter(record.issue, None)
    assert validate(issue_files.SCHEMA, data) == []
    triage_like = {**data, "origin": IssueOrigin.TRIAGE.value}
    assert validate(issue_files.SCHEMA, triage_like) != []
    assert validate(issue_files.SCHEMA, {**triage_like, "problems": ["P-0001"]}) == []


def test_with_autonomy_manual_issues_are_approved_and_prepared(tmp_path):
    world = make_triage_world(tmp_path, gates={"issue-approve": "auto", "plan-confirm": "auto", "fix-session": "auto"})
    prepared = []
    deps = IssueDeps(world.layout, world.config, world.conn, world.clock, world.events, prepare=prepared.append)
    record = IssueService(deps).create_manual("按日期筛选订单", REQUIREMENT, Severity.P2)
    assert prepared == [record.issue.id] and record.issue.status is IssueStatus.TODO
    assert "自动放行(gates.issue-approve 为 auto)" in (world.layout.root / record.path).read_text(encoding="utf-8")
    plain = make_triage_world(tmp_path / "plain")
    deps = IssueDeps(plain.layout, plain.config, plain.conn, plain.clock, plain.events, prepare=prepared.append)
    IssueService(deps).create_manual("按日期筛选订单", REQUIREMENT, Severity.P2)
    assert len(prepared) == 1
