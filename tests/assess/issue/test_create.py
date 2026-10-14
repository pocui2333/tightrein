from dataclasses import replace

import pytest

from tightrein.assess import claims, notes
from tightrein.assess.checks import Snapshot
from tightrein.assess.issue import body, create, files
from tightrein.assess.issue.create import Created, Finding, IssueNotCreated
from tightrein.implement import context as implement_context
from tightrein.implement.context import Risk
from tightrein.implement.design.design import SCHEMA as DESIGN_SCHEMA
from tightrein.implement.prompts import common as implement_common
from tightrein.implement.prompts import design as design_prompt
from tightrein.prompts.build import build
from tightrein.protocol.handoff import load_schema
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
    assert issue.extra["plannedFiles"] == ["services/orders.py"]  # 评估预估要改的文件：调度据此不让改同一文件的并行


def _list_output(good_output, **changes):
    """与 P-0001 同一根因(OrderService.get)、另多一处根因(路由 show)的第二个问题。"""
    output = good_output(
        trigger="登录用户请求订单列表", rootCauses=[{"file": "services/orders.py", "line": 3, "symbol": "OrderService.get"},
                                             {"file": "routes/orders.py", "line": 5, "symbol": "show"}],
        facts=[{"location": "routes/orders.py:5", "observation": "列表直接调用服务的 get，没有带用户条件"}], **changes)
    output["report"] = {**output["report"], "title": "[订单] 订单列表也能读到他人的订单",
                        "summary": "订单列表逐个调用 get，同样没有按用户过滤。",
                        "acceptance": ["用户 A 的订单列表里没有用户 B 的订单"]}
    output["assessment"] = {**output["assessment"], "files": [{"path": "routes/orders.py", "isNew": False}],
                            "direction": "列表按当前用户过滤"}
    return output


def test_an_appended_problem_is_written_into_the_body_and_the_record(runtime, repo, finding, good_output, layout):
    """追加不只写一行历史：问题的内容按新建时的排版写进「追加的问题」，根因与预估文件并进记录，其余正文一字不动。"""
    write(runtime, repo, finding("P-0001"))
    before = files.read_body(layout, "0001")
    second = finding("P-0002", output=_list_output(good_output), title="订单列表越权")
    made = write(runtime, repo, second)
    assert made.appended == "0001" and made.failures == []
    text = files.read_body(layout, "0001")
    sections = body.split(text)
    assert list(sections)[-2:] == ["appended", "history"]  # 在「历史」之前
    assert {key: value for key, value in sections.items() if key not in ("appended", "history")} == {
        key: value for key, value in body.split(before).items() if key != "history"}
    part = sections["appended"]
    assert part.startswith("### P-0002 [订单] 订单列表也能读到他人的订单\n\n#### 结论")
    assert "订单列表逐个调用 get，同样没有按用户过滤。" in part
    assert "触发条件：登录用户请求订单列表" in part
    assert "根因位置：`services/orders.py:3` OrderService.get、`routes/orders.py:5` show" in part
    assert "`routes/orders.py:5` 列表直接调用服务的 get，没有带用户条件" in part
    assert "#### 验收标准\n\n- [ ] 用户 A 的订单列表里没有用户 B 的订单" in part
    assert "修复方向\n\n列表按当前用户过滤" in part
    assert create.check_body(text, "problem", Snapshot(repo)) == []
    # 验收标准：「验收标准」一节之后接着追加问题自己的
    assert body.acceptance_items(text)[0] == "用户 A 请求用户 B 的订单返回 404"
    assert "用户 A 的订单列表里没有用户 B 的订单" in body.acceptance_items(text)
    assert implement_common.acceptance(text) == ["用户 A 请求用户 B 的订单返回 404", "用户 A 的订单列表里没有用户 B 的订单"]
    issue = issues.get(runtime.conn, "0001")
    assert issue.extra["rootKey"] == ["routes/orders.py#show", "services/orders.py#OrderService.get"]
    assert [cause["file"] for cause in issue.extra["rootCauses"]] == ["services/orders.py", "routes/orders.py"]
    assert issue.extra["plannedFiles"] == ["services/orders.py", "routes/orders.py"]
    assert issue.extra["history"][-1]["note"] == "追加问题 P-0002"

    write(runtime, repo, second)  # 已关联的问题再次评估：不重复加入问题列表
    assert issues.get(runtime.conn, "0001").extra["problems"] == ["P-0001", "P-0002"]


def test_a_linked_problem_assessed_again_writes_its_new_evidence(runtime, repo, finding, good_output, layout):
    """建 Issue 的问题回归后重新评估、又判为成立：不重复加入问题列表，新的取证(新的触发条件与事实)写进「追加的问题」，
    标题注明「再次评估」。只写一行历史「追加问题」时，回归后再修的方案被指到正文里并不存在的一段。"""
    first = finding("P-0001")
    write(runtime, repo, first)
    again = _list_output(good_output)
    again["report"] = {**again["report"], "title": "[订单] 回归：订单列表仍能读到他人的订单"}
    made = write(runtime, repo, replace(first, output=again))
    assert made.appended == "0001" and made.failures == []
    issue = issues.get(runtime.conn, "0001")
    assert issue.extra["problems"] == ["P-0001"]
    assert issue.extra["history"][-1]["note"] == "问题 P-0001 再次评估，新的取证写进正文「追加的问题」"
    assert issue.extra["rootKey"] == ["routes/orders.py#show", "services/orders.py#OrderService.get"]
    text = files.read_body(layout, "0001")
    part = body.split(text)["appended"]
    assert part.startswith("### P-0001 [订单] 回归：订单列表仍能读到他人的订单(再次评估)\n\n#### 结论")
    assert "触发条件：登录用户请求订单列表" in part and "`routes/orders.py:5` 列表直接调用服务的 get" in part
    assert implement_common.acceptance(text)[-1] == "用户 A 的订单列表里没有用户 B 的订单"


def test_a_second_appended_problem_follows_the_first_and_user_edits_stay(runtime, repo, finding, good_output, layout):
    write(runtime, repo, finding("P-0001"))
    path = files.body_path(layout, "0001")
    path.write_text(path.read_text(encoding="utf-8").replace("按编号查询订单时没有按用户过滤。", "用户改过的问题描述。"),
                    encoding="utf-8")
    write(runtime, repo, finding("P-0002", output=_list_output(good_output), title="订单列表越权"))
    third = _list_output(good_output)
    third["report"] = {**third["report"], "title": "[订单] 导出也能读到他人的订单", "acceptance": ["导出只含自己的订单"]}
    write(runtime, repo, finding("P-0003", output=third, title="导出越权"))
    text = files.read_body(layout, "0001")
    assert "用户改过的问题描述。" in text and text.count("## 追加的问题") == 1
    assert text.index("### P-0002") < text.index("### P-0003") < text.index("## 历史")
    assert implement_common.acceptance(text)[-1] == "导出只含自己的订单"


def test_an_appended_problem_that_fails_the_checks_fails_the_handoff(runtime, repo, finding, good_output, layout):
    write(runtime, repo, finding("P-0001"))
    output = _list_output(good_output)
    output["report"]["steps"] = ["昨天下午用户 A 打开订单列表"]
    made = write(runtime, repo, finding("P-0002", output=output))
    assert made.failures == ["0001：用了相对日期「昨天」"]
    assert "昨天下午用户 A 打开订单列表" in files.read_body(layout, "0001")  # 与新建时一样：文件保留，交接为 failed


def test_the_code_notes_of_an_appended_problem_are_merged(runtime, repo, finding, good_output, layout):
    write(runtime, repo, finding("P-0001"))
    issue_notes = notes.CodeNotes("0001", "c" * 40)
    issue_notes.add([{"location": "services/orders.py:3", "description": "查询", "role": "core"}], repo)
    notes.save(layout, issue_notes)
    appended = notes.CodeNotes("P-0002", "d" * 40)
    appended.add([{"location": "services/orders.py:3", "description": "重复的位置", "role": "core"},
                  {"location": "routes/orders.py:5", "description": "列表入口", "role": "core"}], repo)
    notes.save(layout, appended)
    write(runtime, repo, finding("P-0002", output=_list_output(good_output)))
    merged = notes.load(layout, "0001")
    assert [entry.description for entry in merged.entries] == ["查询", "列表入口(截取于 commit dddddddddddd)"]
    assert merged.files == ["services/orders.py", "routes/orders.py"] and merged.commit == "c" * 40


def test_the_design_prompt_sees_an_appended_problem(runtime, repo, finding, good_output):
    """实施的方案读正文与验收标准：追加的问题的内容与验收标准都要在提示里(只写一行历史时方案说「内容没有提供」)。"""
    write(runtime, repo, finding("P-0001"))
    write(runtime, repo, finding("P-0002", output=_list_output(good_output)))
    context = implement_context.load(runtime, "0001")
    variables = design_prompt.variables(runtime, context, risk=Risk(False, ()), replan=[], feedback=[],
                                        design_accepted=False)
    assert variables["acceptance"] == "- 用户 A 请求用户 B 的订单返回 404\n- 用户 A 的订单列表里没有用户 B 的订单"
    assert variables["attempt"] == "无"  # 第一次修复
    prompt = build("implement.design", variables, language="zh", schema=load_schema(DESIGN_SCHEMA), tool="claude").text
    assert "## 追加的问题" in prompt and "### P-0002 [订单] 订单列表也能读到他人的订单" in prompt
    assert "触发条件：登录用户请求订单列表" in prompt and "`routes/orders.py:5` 列表直接调用服务的 get" in prompt


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


def test_body_problems_after_creation_are_recorded_and_the_handoff_still_passes(runtime, repo, finding, good_output,
                                                                                layout):
    """建好的 Issue 正文没过检查：Issue 合格、照常放行，只记进交接(bodyProblems 与说明)，不判这一步失败。"""
    output = good_output()
    output["report"]["steps"] = ["昨天下午用户 A 登录", "请求订单详情"]
    found = finding(output=output, title="订单报错，见 services/orders.py:99")  # 问题标题原样写进「引用」
    made = write(runtime, repo, found)
    assert any("相对日期「昨天」" in item for item in made.body_problems)
    assert any("services/orders.py:99" in item for item in made.body_problems)
    assert files.body_path(layout, "0001").is_file() and issues.get(runtime.conn, "0001").status == "todo"

    create.write_handoff(runtime, found, made, None)
    handoff = read_json(layout.issue_dir("0001") / "22-assess.issue-handoff.json")
    assert handoff["status"] == "passed" and handoff["facts"]["failures"] == []
    assert handoff["facts"]["bodyProblems"] == made.body_problems and "正文检查没过" in handoff["notes"]


def test_an_issue_that_was_not_created_still_fails_the_handoff(runtime, finding, layout):
    found = finding()
    create.write_handoff(runtime, found, None, "正文要引用的代码位置不合格")
    handoff = read_json(layout.subject_dir("P-0001") / "22-assess.issue-handoff.json")
    assert handoff["status"] == "failed" and handoff["facts"]["failures"] == ["正文要引用的代码位置不合格"]


def test_urls_in_the_body_are_not_checked_as_code_locations(tmp_path):
    """Issue 0028 的正文：「引用」里的代理地址 http://127.0.0.1:8118 不是代码位置，同一句里真实的位置照常核对。"""
    root = tmp_path / "agy"
    (root / "processors").mkdir(parents=True)
    (root / "processors" / "ai_review.py").write_text("x = 1\n" * 440, encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "config.yaml").write_text("key: value\n" * 100, encoding="utf-8")
    text = body.document("0028 [AI整理] agy 白名单环境变量不全", {
        "problem": "AI 识别笔记和 AI 检查题目调用 agy 时只转发精简白名单里的环境变量。",
        "cause": "- 根因位置：`processors/ai_review.py:255` AGY_ENV_KEYS\n"
                 "- 调用链入口：`processors/ai_review.py:313`、`processors/ai_review.py:379`",
        "acceptance": "- [ ] 在仅设置 ALL_PROXY（不设置 ai_review.proxy）的环境下 agy 子进程能看到 ALL_PROXY",
        "references": "- `config/config.yaml:89` 默认配置设了 ai_review.proxy: http://127.0.0.1:8118，"
                      "按默认配置运行时 agy 的 http(s) 代理会补上\n"
                      "- `processors/ai_review.py:286` subprocess.run(cmd, cwd=cwd, env=env, ...)\n"
                      "- 本地复现：打开 localhost:8080/processors/ai_review.py:313 看到同样的环境变量"}, "zh")
    snapshot = Snapshot(root)
    assert create.check_body(text, "problem", snapshot) == []
    broken = text.replace("ai_review.py:286", "ai_review.py:986")
    assert create.check_body(broken, "problem", snapshot) == [
        "位置 processors/ai_review.py:986：processors/ai_review.py 只有 440 行，引用了第 986 行"]


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
    runtime.settings = kit.make_settings({"boundaries": {"gates": {"issue": "auto_all"}}})
    everything = finding("P-0004", labels=(create.body.DISCUSS,))
    everything.output["rootCauses"] = [{"file": ".github/workflows/ci.yml", "line": 1, "symbol": None}]
    approved = write(runtime, repo, everything)
    assert approved.auto_approved and issues.get(runtime.conn, approved.issues[0]).status == "todo"  # 全自动


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
    assert create.slug(found, 40) == expected


def test_slugs_are_truncated_without_a_trailing_dash_and_fall_back_to_the_source(finding):
    found = finding(source="collect.api_fuzz", check_type="server_error", location="GET /api/order-items/{id}",
                    evidence={"status": 500})
    assert create.slug(found, 15) == "api-order-items"
    assert create.normalize("API Orders---", 6) == "api-or"
    assert create.normalize("abc-def", 4) == "abc"
    empty = finding("P-0002", source="collect.alerts", check_type="", location=None)
    assert create.slug(empty, 40) == ""  # 取不到时留空，分支名由实施阶段另取，不用本地序号凑
    static = finding("P-0003", source="collect.static", check_type="static", location="a.py:3",
                     flags={"symbol": "Runner._acquireLock"})
    assert create.slug(static, 40) == "acquire-lock"  # 不带 static 这种来源名


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
    assert second.extra["blockedBy"] == ["0002"] and second.extra["problems"] == [] and second.status == "todo"
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


def test_relative_dates_are_checked_only_in_the_prose(runtime, repo, finding, good_output):
    """代码里的标识符(`today`)与「引用」中描述代码行为的文字不算相对日期。"""
    output = good_output()
    output["report"]["summary"] = "按 `today` 取当天的记录时没有按用户过滤。"
    output["facts"] = [{"location": "services/orders.py:3", "observation": "删掉今天 id 最大的一条记录"}]
    made = write(runtime, repo, finding(output=output))
    assert not any("相对日期" in item for item in made.body_problems)
