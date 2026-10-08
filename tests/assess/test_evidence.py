import pytest

from tightrein.agents.result import CallStatus
from tightrein.assess import claims, evidence
from tightrein.assess.cases import Case
from tightrein.assess.checks import Snapshot
from tightrein.assess.prompts.claim_verifier import REFUTE, TRIAGE, Inputs, variables
from tightrein.store.tables import occurrences

WORDS = ["可能", "大概"]


@pytest.fixture
def inputs(make_problem, conn):
    problem = make_problem(source="collect.api_fuzz", check_type="server_error", location="GET /orders/{id}")
    claim = claims.build(problem, occurrences.find(conn, problem.id))
    return Inputs("full", claim, "无", "无", "订单数据泄露一律 P0", 80)


def gather(runtime, repo, inputs, *, case=Case.FULL, retries=1, prepared=None, point=TRIAGE):
    return evidence.gather(runtime, point, "P-0001", inputs, Snapshot(repo), case=case, retries=retries,
                           vague_words=WORDS, prepared=prepared)


def test_failed_checks_are_sent_back_and_two_redos_end_unpassed(runtime, repo, inputs, agent, outputs):
    agent.queue(TRIAGE, outputs.confirmed(trigger="可能并发时出现"), CallStatus.SCHEMA_INVALID,
                outputs.confirmed(counterEvidence=[]))
    found = gather(runtime, repo, inputs, retries=2)
    assert not found.passed and len(found.asked) == 3
    assert "[counter-check]" in found.reason  # 重做后仍不过：判证据不足，不硬采纳
    second = agent.calls[1].prompt
    assert "[vague-wording]" in second  # 逐项原因交回同一调用点
    assert "执行没有完成(schema_invalid" in agent.calls[2].prompt  # 执行返回非 ok 也算一次没通过


def test_a_redo_that_passes_stops_the_loop(runtime, repo, inputs, agent, outputs):
    # 两个 orders.py：只写文件名的位置补不全，第一次判不通过
    agent.queue(TRIAGE, outputs.confirmed(facts=[{"location": "orders.py:3", "observation": "x"}]),
                outputs.confirmed())
    found = gather(runtime, repo, inputs, retries=3)
    assert found.passed and len(found.asked) == 2 and agent.calls[1].round == 2
    assert found.failures == [] and found.verdict == "confirmed"


def test_bare_file_names_are_completed_and_unknown_text_references_are_sent_back(runtime, repo, inputs, agent,
                                                                                  outputs):
    agent.queue(TRIAGE, outputs.confirmed(trigger="见 missing.py:3", facts=[
        {"location": "workflows/ci.yml:1", "observation": "隐藏目录下的文件"}]))
    found = gather(runtime, repo, inputs, retries=0)
    assert found.output["facts"][0]["location"] == ".github/workflows/ci.yml:1"
    assert not found.passed and any("missing.py:3" in reason for reason in found.failures)


def test_prepared_output_is_reused_only_when_it_passes(runtime, repo, inputs, agent, outputs):
    found = gather(runtime, repo, inputs, case=Case.FULL, prepared=outputs.confirmed())
    assert found.passed and found.reused and agent.calls == []
    agent.queue(TRIAGE, outputs.confirmed())
    again = gather(runtime, repo, inputs, case=Case.FULL, prepared=outputs.confirmed(counterEvidence=[]))
    assert again.passed and not again.reused and len(agent.calls) == 1


def test_light_and_reproducible_cases_do_not_need_counter_evidence(runtime, repo, inputs, agent, outputs):
    agent.queue(TRIAGE, outputs.confirmed(counterEvidence=[]), outputs.confirmed(counterEvidence=[]))
    assert gather(runtime, repo, inputs, case=Case.LIGHT, retries=0).passed
    assert gather(runtime, repo, inputs, case=Case.REPRODUCIBLE, retries=0).passed


def test_the_refuter_gets_the_same_claim_and_its_own_route(runtime, inputs):
    triage = variables(TRIAGE, inputs, [])
    refuter = variables(REFUTE, inputs, [])
    assert {key: value for key, value in triage.items() if key != "case"} == refuter  # 盲审：输入完全相同
    assert "verdict" not in "".join(refuter.values())
    model = runtime.settings.model_for(REFUTE)
    assert model.alias != runtime.settings.model_for(TRIAGE).alias  # 复核的模型与取证不同
    assert [REFUTE, TRIAGE] in runtime.settings.get("independence")


def test_the_verifier_gets_the_severity_standard_the_project_guide_and_the_title_limit(inputs):
    found = variables(TRIAGE, inputs, ["[verdict] 判定为 'x'"])
    assert found["severity_guide"] == "订单数据泄露一律 P0" and found["title_limit"] == "80"
    assert found["feedback"] == "- [verdict] 判定为 'x'"
    assert variables(TRIAGE, inputs, [])["feedback"] == "无"
