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
    assert set(deep.variables) == {"acceptance", "risk", "diff", "results"}
    assert "订单列表只显示当天" not in "".join(deep.variables.values())
    assert deep.conditions == ("high_risk",)
    # 轻量没通过就不跑深度
    fake = _install(monkeypatch, implement_review=_output(blockers=(_blocker(),)), implement_review_deep=_output())
    failed = review.run(world.runtime, _high_risk(world.context()))
    assert failed.status is Status.FAILED and fake.points() == ["implement.review"]


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
