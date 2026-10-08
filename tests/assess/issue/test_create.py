import pytest

from tightrein.assess import claims, notes
from tightrein.assess.checks import Snapshot
from tightrein.assess.issue import create, files
from tightrein.assess.issue.create import Created, Finding, IssueNotCreated
from tightrein.store.db import transaction
from tightrein.store.files.json import read_json
from tightrein.store.tables import issues, occurrences, problems


@pytest.fixture
def finding(make_problem, conn, good_output):
    def make(problem_id: str = "P-0001", *, severity: str = "P2", size: str = "small", output: dict | None = None,
             labels: tuple[str, ...] = (), task_type: str = "bug", **problem) -> Finding:
        found = make_problem(problem_id, **problem)
        seen = occurrences.find(conn, problem_id)
        return Finding(found, seen[-1], claims.build(found, seen), output or good_output(), severity, size,
                       task_type, "non-core-error", "fix_later", labels, (), "c" * 40)

    return make


def write(runtime, repo, found: Finding) -> Created:
    made = Created()
    with transaction(runtime.conn):
        create.from_problem(runtime, found, Snapshot(repo), made)
    return made


def test_a_problem_with_the_same_root_cause_is_appended_and_raises_the_severity(runtime, repo, finding):
    first = write(runtime, repo, finding("P-0001"))
    assert first.issues == ["0001"] and first.appended is None
    second = write(runtime, repo, finding("P-0002", severity="P1", title="另一个现象"))
    assert second.issues == ["0001"] and second.appended == "0001"
    issue = issues.get(runtime.conn, "0001")
    assert issue.extra["problems"] == ["P-0001", "P-0002"] and issue.severity == "P1"
    assert issue.extra["history"][-1]["note"] == "追加问题 P-0002，严重度由 P2 提升为 P1"
    assert issue.extra["rootKey"] == ["services/orders.py#OrderService.get"]  # 文件#方法，没有方法名时用行号


def test_root_keys_use_the_symbol_or_the_line():
    assert create.root_key([{"file": "a.py", "line": 3, "symbol": "A.b"}, {"file": "a.py", "line": 9, "symbol": None},
                            {"file": "a.py", "line": 4, "symbol": "A.b"}]) == ["a.py#9", "a.py#A.b"]


def test_bare_file_names_are_completed_and_unknown_locations_block_the_issue(runtime, repo, finding, good_output):
    hidden = good_output(rootCauses=[{"file": "workflows/ci.yml", "line": 1, "symbol": None}])
    made = write(runtime, repo, finding(output=hidden))
    assert issues.get(runtime.conn, made.issues[0]).extra["rootCauses"][0]["file"] == ".github/workflows/ci.yml"
    missing = good_output(rootCauses=[{"file": "services/payments.py", "line": 3, "symbol": None}])
    with pytest.raises(IssueNotCreated, match="重新评估"):
        write(runtime, repo, finding("P-0002", output=missing))
    assert len(issues.find(runtime.conn)) == 1


def test_a_document_that_fails_the_checks_is_kept_and_the_handoff_fails(runtime, repo, finding, good_output, layout):
    output = good_output()
    output["report"]["steps"] = ["昨天下午用户 A 登录", "请求订单详情"]
    found = finding(output=output, title="订单报错，见 services/orders.py:99")  # 问题标题原样写进「引用」
    made = write(runtime, repo, found)
    assert any("相对日期「昨天」" in item for item in made.failures)
    assert any("services/orders.py:99" in item for item in made.failures)
    assert files.body_path(layout, "0001").is_file()  # 文件保留
    create.write_handoff(runtime, found, made, None)
    handoff = read_json(layout.issue_dir("0001") / "22-assess.issue-handoff.json")
    assert handoff["status"] == "failed" and handoff["facts"]["failures"]


def test_a_large_problem_spread_over_modules_is_split(runtime, repo, finding, good_output):
    output = good_output(rootCauses=[{"file": "services/orders.py", "line": 3, "symbol": "OrderService.get"},
                                     {"file": "routes/orders.py", "line": 5, "symbol": "show"}])
    output["assessment"]["files"] = [{"path": "services/orders.py", "isNew": False},
                                     {"path": "routes/orders.py", "isNew": False}]
    made = write(runtime, repo, finding(size="large", output=output))
    assert made.issues == ["0001", "0002"]
    first, second = (issues.get(runtime.conn, issue) for issue in made.issues)
    assert first.title.endswith("(services)") and second.title.endswith("(routes)")
    assert first.extra["rootKey"] == ["services/orders.py#OrderService.get"]
    assert second.extra["rootKey"] == ["routes/orders.py#show"]


def test_low_risk_issues_are_approved_automatically_and_the_rest_wait(runtime, repo, finding, kit):
    made = write(runtime, repo, finding("P-0001"))
    assert made.auto_approved and issues.get(runtime.conn, "0001").status == "todo"
    risky = write(runtime, repo, finding("P-0002", location="routes/orders.py:5", task_type="data",
                                         output=None, severity="P2"))
    # 与 0001 同一根因：追加，不新建
    assert risky.appended == "0001"
    runtime.settings = kit.make_settings({"boundaries": {"gates": {"issue": "manual"}}})
    output_finding = finding("P-0003", labels=(create.body.DISCUSS,))
    output_finding.output["rootCauses"] = [{"file": "routes/orders.py", "line": 4, "symbol": None}]
    gated = write(runtime, repo, output_finding)
    issue = issues.get(runtime.conn, gated.issues[0])
    assert issue.status == "needs_decision" and issue.gate == "issue" and not gated.auto_approved
    assert issue.extra["labels"] == [create.body.DISCUSS]


@pytest.mark.parametrize("problem, latest, expected", [
    ({"source": "collect.api_fuzz", "check_type": "server_error", "location": "GET /api/orders/{id}"},
     {"evidence": {"status": 500}}, "api-orders-500"),
    ({"source": "collect.platform_errors", "check_type": "error", "location": "OrderService.Get"},
     {"evidence": {"exceptionType": "System.NullReferenceException"}, "flags": {"symbol": "OrderService.Get"}},
     "nullreferenceexception-get"),
    ({"source": "collect.incidental", "check_type": "incidental:error-handling", "location": "a.py:3"},
     {"flags": {"symbol": "Saver.save"}}, "error-handling-save"),
])
def test_slugs_per_probe(finding, problem, latest, expected):
    found = finding(evidence=latest.get("evidence"), flags=latest.get("flags"), **problem)
    assert create.slug(found, 40, 1) == expected


def test_slugs_are_truncated_without_a_trailing_dash_and_fall_back_to_the_source(finding):
    found = finding(source="collect.api_fuzz", check_type="server_error", location="GET /api/order-items/{id}",
                    evidence={"status": 500})
    assert create.slug(found, 15, 1) == "api-order-items"
    assert create.normalize("API Orders---", 6) == "api-or"
    assert create.normalize("abc-def", 4) == "abc"
    empty = finding("P-0002", source="collect.alerts", check_type="", location=None)
    assert create.slug(empty, 40, 7) == "alerts-7"


def test_code_notes_follow_the_problem_to_the_issue(runtime, repo, finding, layout):
    found = finding()
    problem_notes = notes.CodeNotes("P-0001", "c" * 40)
    problem_notes.add([{"location": "services/orders.py:3", "description": "查询", "role": "core"}], repo)
    notes.save(layout, problem_notes)
    write(runtime, repo, found)
    assert notes.load(layout, "0001").entries == problem_notes.entries


def test_manual_issue_is_todo_without_problems(runtime, layout):
    description = "## 背景\n导出订单时要带上备注\n\n## 验收标准\n- [ ] 导出的表格有备注列\n- 备注为空时留空\n"
    issue_id = create.new_issue(runtime, description, severity=None, kind="feature")
    issue = issues.get(runtime.conn, issue_id)
    assert (issue.status, issue.origin, issue.kind, issue.extra["problems"]) == ("todo", "user", "feature", [])
    assert issue.extra["approvedAt"] and issue.extra["splitHint"] is None
    body = files.read_body(layout, issue_id)
    assert "**背景**" in body and "## 背景" not in body  # 用户原文中的小标题降为加粗
    assert "- [ ] 导出的表格有备注列\n- [ ] 备注为空时留空" in body
    assert read_json(layout.issue_dir(issue_id) / "22-assess.issue-handoff.json")["facts"]["issues"] == [issue_id]


def test_a_requirement_with_too_many_criteria_gets_a_split_hint(runtime):
    description = "做一批改动\n\n## 验收标准\n" + "".join(f"- 第 {number} 条\n" for number in range(1, 8))
    issue = issues.get(runtime.conn, create.new_issue(runtime, description, severity="P3", kind="feature"))
    assert "超过 5 条" in issue.extra["splitHint"]
    with pytest.raises(ValueError):
        create.new_issue(runtime, "  \n ", severity=None, kind="feature")


def test_split_back_creates_ordered_issues_and_cancels_the_original(runtime, repo, finding):
    write(runtime, repo, finding())
    created = create.split_back(runtime, "0001", ["先改查询\n细节", "再改导出"])
    assert created == ["0002", "0003"]
    original = issues.get(runtime.conn, "0001")
    assert original.status == "cancelled" and original.extra["closeReason"] == "split"
    first, second = (issues.get(runtime.conn, issue) for issue in created)
    assert first.title == "先改查询" and first.extra["problems"] == ["P-0001"] and first.extra["rootKey"]
    assert second.extra["dependsOn"] == "0002" and second.extra["problems"] == [] and second.status == "todo"
    assert first.extra["approvedAt"] == original.extra["approvedAt"]  # 排队位置跟着原 Issue
    assert problems.get(runtime.conn, "P-0001").issue == "0002"  # 关联问题改挂到拆出的第一个 Issue


def test_a_failed_split_leaves_no_new_issue_files(runtime, repo, finding, layout, monkeypatch):
    write(runtime, repo, finding())

    def broken(*args, **kwargs):
        raise RuntimeError("写到一半")

    monkeypatch.setattr(create, "apply_event", broken)
    with pytest.raises(RuntimeError):
        create.split_back(runtime, "0001", ["先改查询", "再改导出"])
    assert not files.record_path(layout, "0002").exists() and not files.body_path(layout, "0003").exists()
    assert issues.get(runtime.conn, "0002") is None and issues.get(runtime.conn, "0001").status == "todo"
