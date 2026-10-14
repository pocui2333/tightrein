from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tightrein.agents.result import CallResult, CallStatus
from tightrein.collect.incidental.handoffs import FINDING_SCHEMA
from tightrein.implement.check.findings import LOCAL, PLAN_GAP
from tightrein.implement.context import Decision
from tightrein.implement.review import review
from tightrein.protocol.handoff import Status, load_schema, schema_errors
from tightrein.protocol.naming import format_iso

FIX = {"src/orders.py": "def recent(days):\n    return days * 7\n"}
FIX_TEST = "\n\ndef test_fix_side():\n    assert recent(7) == 49\n"
CRITERIA = ("列表显示最近 7 天的订单", "接口 GET /api/orders 返回 200")
INCIDENTAL = {"file": "src/users.py", "line": 1, "symbol": None, "category": "defect", "confidence": "suspected",
              "evidence": "NAME 写死为 'a'", "text": "用户名写死"}


def _blocker(**fields: Any) -> dict[str, Any]:
    found = {"kind": "root-cause-unfixed", "location": "src/orders.py:2", "trigger": "days 为 0 时",
             "problem": "仍只返回当天", "category": "local", "rootCause": "没有改到查询条件"}
    found.update(fields)
    return found


def _output(*, blockers: tuple[dict[str, Any], ...] = (), results: tuple[str, ...] = ("pass", "pass"),
            previous: tuple[dict[str, Any], ...] = (), incidental: tuple[dict[str, Any], ...] = (),
            suggestions: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"analysis": "逐条看过", "previousBlockers": list(previous), "blockers": list(blockers),
            "acceptance": [{"criterion": criterion, "result": result, "reason": "看了代码"}
                           for criterion, result in zip(CRITERIA, results, strict=True)],
            "unverified": [], "incidentalFindings": list(incidental), "knowledgeSuggestions": list(suggestions)}


class FakeAsk:
    """按调用点给出预置结果；每份输出都先按 review.schema.json 校验(模型的输出经 agents.call 校验后才到这里)。"""

    def __init__(self, **outputs: dict[str, Any] | None) -> None:
        self.outputs = outputs
        self.requests: list[Any] = []
        self.schema = load_schema(review.SCHEMA)

    def __call__(self, runtime: Any, context: Any, request: Any, usage: Any) -> CallResult:
        self.requests.append(request)
        output = self.outputs[request.point.replace(".", "_")]
        if output is None:
            return CallResult(CallStatus.FAILED, "claude", "opus", error="超时")
        assert schema_errors(output, self.schema) == []
        return CallResult(CallStatus.OK, "claude", "opus", output=output)

    def points(self) -> list[str]:
        return [request.point for request in self.requests]


@pytest.fixture(autouse=True)
def _deep_only_when_risky(world: Any, settings_with: Any) -> None:
    """本文件的大多数用例只看轻量审查：关掉「深度审查每次都做」，另有用例专门测它。"""
    world.runtime.settings = settings_with({"controls": {"implement.review": {"deepAlways": False}}})


def _install(monkeypatch: pytest.MonkeyPatch, **outputs: dict[str, Any] | None) -> FakeAsk:
    fake = FakeAsk(**outputs)
    monkeypatch.setattr(review, "ask", fake)
    return fake


def _high_risk(context: Any) -> Any:
    context.issue = replace(context.issue, extra={**context.issue.extra, "flags": {"dataStructure": True}})
    return context


def test_findings_without_location_or_trigger_are_discarded(world: Any) -> None:
    world.repo.write(FIX)
    output = _output(blockers=(
        _blocker(),
        _blocker(kind="style"),
        _blocker(kind="authz"),  # 只有深度审查可报
        _blocker(location=None),
        _blocker(location="src/orders.py:99"),
        _blocker(location="src/missing.py:1"),
        _blocker(location="../outside.py:1"),
        _blocker(trigger="  "),
    ))
    kept, discarded = review.filter_findings(output, world.repo.path, review.LIGHT)
    assert [(item.kind, item.location) for item in kept] == [("root-cause-unfixed", "src/orders.py:2")]
    assert kept[0].trigger == "days 为 0 时" and "没有改到查询条件" in kept[0].summary
    reasons = [item["reason"] for item in discarded]
    assert len(reasons) == 7
    assert "不在轻量审查允许的范围内" in reasons[0] and "不在轻量审查允许的范围内" in reasons[1]
    assert reasons[2] == "没有给出「文件路径:行号」"
    # 位置核对与评估共用 assess/checks.Snapshot.problem，原因用它的文字
    assert reasons[3] == "位置不存在：src/orders.py 只有 2 行，引用了第 99 行"
    assert reasons[4] == "位置不存在：src/missing.py 在代码中不存在"
    assert reasons[5] == "位置不存在：../outside.py 在代码中不存在"
    assert reasons[6] == "没有给出触发条件"
    deep_kept, _ = review.filter_findings(_output(blockers=(_blocker(kind="authz"),)), world.repo.path, review.DEEP)
    assert [item.kind for item in deep_kept] == ["authz"]


def test_a_clean_review_passes_and_writes_the_facts_status_and_collection_read(world: Any,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write(FIX)
    fake = _install(monkeypatch, implement_review=_output(incidental=(INCIDENTAL,), suggestions=("订单按天查询",)))
    result = review.run(world.runtime, world.context())
    assert result.status is Status.PASSED and fake.points() == ["implement.review"]
    facts = result.facts
    assert facts["diffHash"] == world.git().diff_hash(world.repo.base)
    assert facts["base"] == facts["baseCommit"] == world.repo.base
    assert facts["blockers"] == [] and facts["firstCategory"] is None and facts["misjudged"] is None
    assert facts["skipped"] is None and facts["knowledgeSuggestions"] == ["订单按天查询"]
    # 任务外发现照 collect/incidental/finding.schema.json 的结构，采集据 baseCommit 取 commit
    finding_schema = load_schema(FINDING_SCHEMA)
    assert facts["incidentalFindings"] == [INCIDENTAL]
    assert all(schema_errors(item, finding_schema) == [] for item in facts["incidentalFindings"])
    assert facts["modes"] == ["light"] and facts["incremental"] is False


def test_blockers_carry_location_kind_and_summary(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write(FIX)
    _install(monkeypatch, implement_review=_output(blockers=(_blocker(), _blocker(location=None))))
    result = review.run(world.runtime, world.context())
    assert result.status is Status.FAILED
    [blocker] = result.facts["blockers"]
    assert {"location", "kind", "summary"} <= set(blocker)
    assert (blocker["location"], blocker["kind"], blocker["category"]) == ("src/orders.py:2", "root-cause-unfixed",
                                                                           LOCAL)
    assert len(result.facts["discardedFindings"]) == 1 and result.facts["firstCategory"] == LOCAL


def test_failed_acceptance_without_a_blocker_is_a_plan_gap(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write(FIX)
    _install(monkeypatch, implement_review=_output(results=("fail", "pass")))
    result = review.run(world.runtime, world.context())
    [blocker] = result.facts["blockers"]
    assert blocker["kind"] == "requirement-unmet" and blocker["category"] == PLAN_GAP
    assert CRITERIA[0] in blocker["location"]


def test_unfollowed_decisions_do_not_stand_for_failed_acceptance(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """没照用户的决定做(decision-unmet)不对应验收标准：同时有判为不满足的验收标准时照常按方案缺口记一条；
    对应验收标准的 requirement-unmet 才抵掉它。"""
    world.repo.write(FIX)
    decision = _blocker(kind="decision-unmet", problem="用户定了只看 7 天，改动按方案放宽到 30 天")
    _install(monkeypatch, implement_review=_output(blockers=(decision,), results=("fail", "pass")))
    result = review.run(world.runtime, world.context())
    found = [(item["kind"], item["category"]) for item in result.facts["blockers"]]
    assert found == [("decision-unmet", LOCAL), ("requirement-unmet", PLAN_GAP)]
    assert CRITERIA[0] in result.facts["blockers"][1]["location"] and result.facts["firstCategory"] == PLAN_GAP
    covered = _blocker(kind="requirement-unmet", category="plan_gap")
    _install(monkeypatch, implement_review=_output(blockers=(decision, covered), results=("fail", "pass")))
    result = review.run(world.runtime, world.context())
    assert [item["kind"] for item in result.facts["blockers"]] == ["decision-unmet", "requirement-unmet"]


def _diverged(world: Any, main_tests: str) -> tuple[str, str]:
    """修复分支在 tests/test_orders.py 末尾追加了测试并提交，main 把这个文件改成 main_tests；切回修复分支。
    返回(修复分支的 HEAD, main)。"""
    repo = world.repo
    base = (repo.path / "tests/test_orders.py").read_text(encoding="utf-8")
    repo.git("checkout", "-q", "-b", "fix/18-orders")
    fixed = repo.commit("fix: 订单按天数查询", {**FIX, "tests/test_orders.py": base + FIX_TEST})
    repo.git("checkout", "-q", "main")
    main = repo.commit("test: 补主干一侧的测试", {"tests/test_orders.py": main_tests.format(base=base)})
    repo.git("checkout", "-q", "fix/18-orders")
    return fixed, main


def test_both_reviews_get_what_main_brought_into_a_resolved_conflict(world: Any, settings_with: Any,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """发布合并 main 冲突、退回编码解决：两侧都在测试文件末尾追加了测试，解决时只留了修复一侧。改动相对合并进来的
    main 算，丢掉的 main 一侧只是一处删除；两种审查都拿到冲突文件、main 一侧的提交与改动，才认得出它。"""
    world.runtime.settings = settings_with()  # 深度审查每次都做
    fixed, main = _diverged(world, "{base}\n\ndef test_main_side():\n    assert recent(2) == 2\n")
    with pytest.raises(AssertionError):
        world.repo.git("merge", "--no-ff", "--no-edit", main)
    resolved = (world.repo.path / "tests/test_orders.py").read_text(encoding="utf-8").split("<<<<<<<")[0] + FIX_TEST
    world.repo.commit("Merge origin/main into fix/18-orders", {"tests/test_orders.py": resolved})
    returned = world.handoff("release.pr", {"backTo": "implement.code", "main": main, "conflicts": ["tests/test_orders.py"],
                                            "mainCommits": [], "report": "冲突报告", "returns": 1,
                                            "worktreeCommit": fixed})
    fake = _install(monkeypatch, implement_review=_output(), implement_review_deep=_output())
    review.run(world.runtime, replace(world.context(), base_commit=main, returned=returned))
    assert fake.points() == ["implement.review", "implement.review.deep"]
    assert "-def test_main_side():" in fake.requests[0].variables["diff"]
    for request in fake.requests:
        text = request.variables["merge"]
        assert "tests/test_orders.py" in text and f"{main[:12]} test: 补主干一侧的测试" in text
        assert "+def test_main_side():" in text and "test_fix_side" not in text
        assert "合并时冲突，由编码解决" in text


def test_a_release_merge_that_touched_the_fixed_files_is_shown_too(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """发布合并 main 没有冲突、但 main 改到了修复的文件(退回自检)：同样给出合并信息；没有退回时为「无」。"""
    _, main = _diverged(world, "import os\n{base}")
    world.repo.git("merge", "--no-ff", "--no-edit", main)
    merged = world.repo.git("rev-parse", "HEAD").strip()
    returned = world.handoff("release.pr", {"backTo": "implement.check", "syncedMain": main, "merged": merged,
                                            "overlap": ["tests/test_orders.py"], "resolutions": {},
                                            "worktreeCommit": merged})
    fake = _install(monkeypatch, implement_review=_output())
    review.run(world.runtime, replace(world.context(), base_commit=main, returned=returned))
    text = fake.requests[0].variables["merge"]
    assert "发布合并时 git 自动合并" in text and "+import os" in text and "test_fix_side" not in text
    assert f"{main[:12]} test: 补主干一侧的测试" in text
    fake = _install(monkeypatch, implement_review=_output())
    review.run(world.runtime, replace(world.context(), base_commit=main))
    assert fake.requests[0].variables["merge"] == "无"


def test_the_side_a_person_chose_at_release_counts_like_a_user_decision(world: Any, settings_with: Any,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """退回编码已达上限、交给人：两侧是同一功能的互斥实现，人整份取了修复一侧(发布记下 resolutions)，退回自检后审查。
    人的取舍与用户的决定一样作数：两种审查都拿到取了哪一侧，main 一侧对这个文件的改动不再列给它核对，免得报
    main-dropped、编码把人舍弃的实现加回来；两侧合并的照常列 main 一侧，并指向人的合并提交。"""
    world.runtime.settings = settings_with()  # 深度审查每次都做
    fixed, main = _diverged(world, "{base}\n\ndef test_main_side():\n    assert recent(2) == 2\n")
    with pytest.raises(AssertionError):
        world.repo.git("merge", "--no-ff", "--no-edit", main)
    world.repo.commit("Merge origin/main into fix/18-orders",
                      {"tests/test_orders.py": world.repo.git("show", f"{fixed}:tests/test_orders.py")})
    merged = world.repo.git("rev-parse", "HEAD").strip()
    facts = {"backTo": "implement.check", "syncedMain": main, "merged": merged, "overlap": [], "worktreeCommit": merged}

    def reviewed(side: str) -> FakeAsk:
        fake = _install(monkeypatch, implement_review=_output(), implement_review_deep=_output())
        returned = world.handoff("release.pr", {**facts, "resolutions": {"tests/test_orders.py": side}})
        review.run(world.runtime, replace(world.context(), base_commit=main, returned=returned))
        assert fake.points() == ["implement.review", "implement.review.deep"]
        return fake

    fake = reviewed("fix-side")
    assert "-def test_main_side():" in fake.requests[0].variables["diff"]
    for request in fake.requests:
        text = request.variables["merge"]
        assert "人在发布阶段解决冲突时的取舍(与用户的决定一样作数)：tests/test_orders.py 整份取了修复一侧" in text
        assert "test_main_side" not in text and "提交：无" in text
    for request in reviewed("both").requests:
        text = request.variables["merge"]
        assert f"人合并的结果在提交 {merged[:12]}" in text and "+def test_main_side():" in text
        assert f"{main[:12]} test: 补主干一侧的测试" in text


def test_the_side_the_user_chose_at_the_coding_gate_counts_like_a_user_decision(world: Any, settings_with: Any,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """发布合并 main 冲突、退回编码：main 删了修复改过的文件，改文件解决不了，用户在编码关卡上选保留修复一侧
    (approve --option 1)，程序写成修复一侧、完成合并；之后的轮次照抄编码交接的 mergedMain。用户的决定里它只是
    「implement.code，选项 1」：两种审查都拿到这是用户的决定、对应哪个文件，main 一侧对它的改动不再列给审查核对，
    免得报 main-dropped、编码推翻用户的选择；选 main 一侧的照常列出。合并的不是这个 main 时不算。"""
    world.runtime.settings = settings_with()  # 深度审查每次都做
    repo = world.repo
    repo.git("checkout", "-q", "-b", "fix/18-orders")
    fixed = repo.commit("fix: 订单按天数查询", FIX)
    repo.git("checkout", "-q", "main")
    main = repo.commit("refactor: 去掉旧的订单模块", {"src/orders.py": None})
    repo.git("checkout", "-q", "fix/18-orders")
    with pytest.raises(AssertionError):
        repo.git("merge", "--no-ff", "--no-edit", main)
    repo.commit("Merge origin/main into fix/18-orders", FIX)  # 按用户的选择写成修复一侧
    merged = repo.git("rev-parse", "HEAD").strip()
    returned = world.handoff("release.pr", {"backTo": "implement.code", "main": main, "conflicts": ["src/orders.py"],
                                            "mainCommits": [], "worktreeCommit": fixed})
    unmergeable = [{"path": "src/orders.py", "kind": "修复改了这个文件，main 删了它(或改了名)"}]

    def reviewed(side: str, chosen_main: str = main) -> list[str]:
        coded = world.handoff("implement.code", {"mergedMain": {
            "main": chosen_main, "conflicts": [], "unmergeable": unmergeable, "side": side, "commit": merged}})
        fake = _install(monkeypatch, implement_review=_output(), implement_review_deep=_output())
        context = world.context(latest={"implement.code": coded}, decisions=[
            Decision("implement.code", "approve", 1 if side == "fix" else 2, None, "2026-10-08T03:00:00Z")])
        review.run(world.runtime, replace(context, base_commit=main, returned=returned))
        assert fake.points() == ["implement.review", "implement.review.deep"]
        return [request.variables["merge"] for request in fake.requests]

    for text in reviewed("fix"):
        assert "改文件解决不了的按用户选的一侧写成，见下一条" in text
        assert "用户在编码关卡上为改文件解决不了的冲突选的一侧(这是用户的决定：" in text
        assert "src/orders.py 整份取了修复一侧" in text and "不报 `main-dropped`" in text
        assert "-def recent" not in text and "提交：无" in text and "人在发布阶段" not in text
    for text in reviewed("main"):
        assert "src/orders.py 整份取了 main 一侧" in text
        assert "-def recent(days):" in text and f"{main[:12]} refactor: 去掉旧的订单模块" in text
    for text in reviewed("fix", chosen_main="f" * 40):  # 编码交接记的是别的 main
        assert "用户在编码关卡上" not in text and "-def recent(days):" in text


def test_the_deep_review_gets_the_questions_the_user_answered(world: Any, settings_with: Any,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """深度审查拿不到方案：方案交给用户拍板的问题与推荐随用户的决定给它，才核对得了「第二点按 B」照做没有。"""
    world.runtime.settings = settings_with()  # 深度审查每次都做
    world.repo.write(FIX)
    plan = world.handoff("implement.design", {"summary": "改为最近 7 天", "userDecisions": [
        {"question": "第二点：分页用 A(每页 20)还是 B(每页 50)", "recommendation": "A", "reason": "改动最小"}]})
    decision = Decision("implement.approve", "approve", None, "同意，第二点按 B", "2026-10-08T03:00:00Z")
    fake = _install(monkeypatch, implement_review=_output(), implement_review_deep=_output())
    review.run(world.runtime, world.context(latest={"implement.design": plan}, decisions=[decision]))
    text = fake.requests[1].variables["decisions"]
    assert "同意，第二点按 B" in text and "- 第二点：分页用 A(每页 20)还是 B(每页 50)(方案推荐：A)" in text
    assert "改动最小" not in text


def test_criteria_confirmed_after_deploy_are_not_judged_here(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """部署后才能确认的标准不列给审查；模型仍给了结论(哪怕无法判断、不满足)也丢掉：不因它不通过、不因它等用户。"""
    observed = "部署后的观察期与之后的覆盖运行中不再出现指纹为 `orders:fp` 的问题"
    world.repo.write(FIX)
    output = _output()
    output["acceptance"] += [{"criterion": observed, "result": "unknown", "reason": "要部署后才知道"},
                             {"criterion": f"[ ] {observed}", "result": "fail", "reason": "还没部署"}]
    fake = _install(monkeypatch, implement_review=output)
    body = world.context().body + f"- [ ] {observed}\n"
    result = review.run(world.runtime, world.context(body=body))
    assert observed not in fake.requests[0].variables["acceptance"]
    assert result.status is Status.PASSED and result.facts["awaitingUser"] == [] and result.facts["blockers"] == []
    assert [item["criterion"] for item in result.facts["acceptance"]] == list(CRITERIA)


def test_deep_review_runs_only_after_the_light_one_passes_and_is_blind(world: Any,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write(FIX)
    fake = _install(monkeypatch, implement_review=_output(), implement_review_deep=_output())
    result = review.run(world.runtime, _high_risk(world.context()))
    assert result.status is Status.PASSED and fake.points() == ["implement.review", "implement.review.deep"]
    assert result.facts["modes"] == ["light", "deep"] and result.facts["risk"]["high"] is True
    deep = fake.requests[1]
    # 盲审：只给验收标准、风险类别、最终改动与检查输出，不给 Issue 正文、方案与代码笔记
    assert set(deep.variables) == {"acceptance", "decisions", "merge", "risk", "diff", "results"}
    assert "订单列表只显示当天" not in "".join(deep.variables.values()) and deep.variables["merge"] == "无"
    assert deep.conditions == ("high_risk",)
    # 轻量没通过就不跑深度
    fake = _install(monkeypatch, implement_review=_output(blockers=(_blocker(),)), implement_review_deep=_output())
    failed = review.run(world.runtime, _high_risk(world.context()))
    assert failed.status is Status.FAILED and fake.points() == ["implement.review"]


def test_deep_review_runs_every_time_by_default(world: Any, settings_with: Any,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    world.runtime.settings = settings_with()
    world.repo.write(FIX)
    fake = _install(monkeypatch, implement_review=_output(), implement_review_deep=_output())
    result = review.run(world.runtime, world.context())
    assert result.status is Status.PASSED and fake.points() == ["implement.review", "implement.review.deep"]
    assert result.facts["risk"]["high"] is False  # 不是高风险也做深度审查


def test_unknown_items_wait_for_the_user_and_the_decision_is_not_rerun(world: Any,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write(FIX)
    fake = _install(monkeypatch, implement_review=_output(results=("pass", "unknown")))
    waiting = review.run(world.runtime, world.context())
    assert waiting.status is Status.PENDING and waiting.facts["awaitingUser"][0]["criterion"] == CRITERIA[1]
    assert world.runtime.workspace.human_document("0018", "pending").is_file()
    later = format_iso(world.clock.now() + timedelta(minutes=5))
    approved = [Decision("implement.review", "approve", None, "本机看过返回 200", later)]
    passed = review.run(world.runtime, world.context(latest={"implement.review": waiting}, decisions=approved))
    assert passed.status is Status.PASSED and len(fake.requests) == 1
    assert passed.facts["awaitingUser"] == []
    assert passed.facts["unverified"] == [{"item": CRITERIA[1], "reason": "用户判断：本机看过返回 200"}]
    rejected = [Decision("implement.review", "reject", None, "返回了 500", later)]
    failed = review.run(world.runtime, world.context(latest={"implement.review": waiting}, decisions=rejected))
    assert failed.status is Status.FAILED and failed.facts["firstCategory"] == LOCAL
    assert "返回了 500" in failed.facts["blockers"][0]["summary"] and len(fake.requests) == 1


def test_a_review_without_a_result_is_marked_for_a_review_only_rerun(world: Any,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write(FIX)
    _install(monkeypatch, implement_review=None)
    result = review.run(world.runtime, world.context())
    assert result.status is Status.FAILED and result.facts["callFailed"] is True
    assert result.facts["blockers"] == [] and "只重跑审查" in result.summary
    # 调用失败后的下一次照常审全部
    fake = _install(monkeypatch, implement_review=_output())
    again = review.run(world.runtime, world.context(latest={"implement.review": result}))
    assert again.status is Status.PASSED and again.facts["incremental"] is False
    assert "本轮审查全部改动" in fake.requests[0].variables["scope"]


def test_an_unchanged_diff_reuses_the_passed_review(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write(FIX)
    fake = _install(monkeypatch, implement_review=_output(incidental=(INCIDENTAL,)))
    first = review.run(world.runtime, world.context())
    again = review.run(world.runtime, world.context(round=2, latest={"implement.review": first}))
    assert again.status is Status.PASSED and len(fake.requests) == 1
    assert again.facts["skipped"] == "改动没变，沿用上次审查结论" and again.round == 2
    # 任务外发现已记在上次的交接里，沿用时不重复交给采集
    assert first.facts["incidentalFindings"] == [INCIDENTAL] and again.facts["incidentalFindings"] == []


def test_correction_rounds_review_only_new_changes_and_record_false_blocks(world: Any,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write({**FIX, "src/users.py": "NAME = 'b'\n"})
    _install(monkeypatch, implement_review=_output(blockers=(_blocker(),)))
    first = review.run(world.runtime, world.context())
    assert first.status is Status.FAILED
    world.repo.write({"src/orders.py": "def recent(days):\n    return max(days, 1) * 7\n"})
    previous = ({"location": "src/orders.py:2", "kind": "root-cause-unfixed", "verdict": "misjudged",
                 "reason": "days 为 0 时本来就不会调用"},)
    fake = _install(monkeypatch, implement_review=_output(previous=previous))
    second = review.run(world.runtime, world.context(round=2, latest={"implement.review": first}))
    assert second.status is Status.PASSED
    request = fake.requests[0]
    assert second.facts["incremental"] is True and second.facts["reviewedFiles"] == ["src/orders.py"]
    assert "src/users.py" not in request.variables["diff"] and "src/orders.py" in request.variables["diff"]
    assert "root-cause-unfixed" in request.variables["previous"]
    misjudged = second.facts["misjudged"]
    assert misjudged["kind"] == "false_block" and misjudged["point"] == "implement.review"
    assert "src/orders.py:2" in misjudged["detail"]


def test_location_problem(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    assert review.location_problem(tmp_path, "a.py:2") is None
    assert review.location_problem(tmp_path, "`a.py:1-2`") is None
    assert "不是「文件路径:行号」" in (review.location_problem(tmp_path, "a.py") or "")
    assert "只有 2 行" in (review.location_problem(tmp_path, "a.py:3") or "")
