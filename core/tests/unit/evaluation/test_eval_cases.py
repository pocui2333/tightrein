import json
import re
import shutil

import pytest
from eval_world import handoff

from tightrein.domain.enums import ScoreResult, Stage
from tightrein.evaluation import manifest
from tightrein.evaluation.cases import add_case, verify_cases
from tightrein.evaluation.errors import CaseProblem, EvalCaseInvalid, EvalCaseTampered, SealRefused
from tightrein.observability import events

RETRIEVAL = {"id": "E-0001", "kind": "retrieval", "query": "公司过滤", "filters": {"types": ["defect-pattern"]},
             "expected": ["DP-0001"], "source": "R-20261001-010000-triage"}


def rubric_items(stage):
    return {"triage.evidence-location", "triage.no-vague-wording"}


def problems(raised):
    return [(problem.case, problem.reason) for problem in raised.value.problems]


def test_a_sealed_and_committed_set_passes(world):
    world.module_case("E-0001", expected={"excludeItems": [{"itemId": "triage.no-vague-wording", "reason": "无结论"}],
                                          "assertions": [{"path": "verdict", "op": "equals", "value": "confirmed",
                                                          "description": "判定成立"}]},
                      replayExpectation=[{"itemId": "triage.evidence-location", "result": "pass"}])
    world.module_case("E-0001", module="fix", commit="def5678")
    world.retrieval_cases(RETRIEVAL)
    world.sealed()
    verified = verify_cases(world.layout, world.process, Stage.TRIAGE, rubric_items)
    assert [case.key for case in verified.module_cases] == ["triage/E-0001"]
    case = verified.module_cases[0]
    assert (case.handoff, case.commit, case.exclude_items) == ("handoff.json", "abc1234", {
        "triage.no-vague-wording": "无结论"})
    assert [(item.item_id, item.path, item.op, item.value) for item in case.assertions] == [
        ("assert-1", "verdict", "equals", "confirmed")]
    assert case.replay_expectation == {"triage.evidence-location": ScoreResult.PASS}
    assert case.input_file.is_file() and case.gates_file is None and case.replay_dir is None
    assert set(verified.case_hashes) == {"fix/E-0001", "retrieval/cases.jsonl", "triage/E-0001"}
    assert verified.manifest_sha256 == manifest.file_sha256(world.layout.eval_manifest())
    assert re.fullmatch(r"[0-9a-f]{40}", verified.evals_tree)
    [retrieval] = verified.retrieval_cases
    assert (retrieval.id, retrieval.filters.types, retrieval.expected) == ("E-0001", ("defect-pattern",), ("DP-0001",))


def test_case_hash_covers_paths_and_contents(tmp_path):
    first = tmp_path / "a"
    (first / "input").mkdir(parents=True)
    (first / "input" / "x.json").write_text("1", encoding="utf-8")
    base = manifest.case_sha256(first)
    (first / "input" / "x.json").rename(first / "input" / "y.json")
    assert manifest.case_sha256(first) != base
    (first / "input" / "y.json").rename(first / "input" / "x.json")
    assert manifest.case_sha256(first) == base


def test_changed_extra_and_missing_cases_are_all_listed(world):
    world.module_case("E-0001")
    world.module_case("E-0002")
    world.sealed()
    (world.layout.evals_dir() / "triage" / "E-0001" / "input" / "handoff.json").write_text("{}", encoding="utf-8")
    world.module_case("E-0003")
    shutil.rmtree(world.layout.evals_dir() / "triage" / "E-0002")
    world.commit()
    with pytest.raises(EvalCaseTampered) as raised:
        verify_cases(world.layout, world.process)
    assert problems(raised) == [
        ("triage/E-0001", "内容与封存时不同"),
        ("triage/E-0002", "manifest.json 中登记了但目录或文件不存在"),
        ("triage/E-0003", "没有登记在 manifest.json 中"),
    ]
    assert "tightrein eval seal" in str(raised.value)


def test_uncommitted_changes_are_refused_even_when_resealed(world):
    world.module_case("E-0001")
    world.sealed()
    world.module_case("E-0002")
    world.seal()
    with pytest.raises(EvalCaseTampered) as raised:
        verify_cases(world.layout, world.process)
    assert problems(raised) == [
        ("workspaces/sample/evals/manifest.json", "未提交的改动(M)"),
        ("workspaces/sample/evals/triage/E-0002/case.json", "未提交的改动(??)"),
        ("workspaces/sample/evals/triage/E-0002/input/handoff.json", "未提交的改动(??)"),
    ]


def test_invalid_cases_are_listed(world):
    world.module_case("E-0001", title="")
    world.module_case("E-0002", id="E-0009")
    world.module_case("E-0003", module="fix", input={"handoff": "missing.json", "commit": "abc1234"})
    world.module_case("E-0004", expected={"excludeItems": [{"itemId": "triage.unknown", "reason": "x"}]})
    world.retrieval_cases(RETRIEVAL, RETRIEVAL, {**RETRIEVAL, "id": "E-0002", "filters": {"types": ["lesson"]}})
    world.sealed()
    with pytest.raises(EvalCaseInvalid) as raised:
        verify_cases(world.layout, world.process, rubric_items=rubric_items)
    assert problems(raised) == [
        ("fix/E-0003", "输入文件 input/missing.json 不存在"),
        ("retrieval/cases.jsonl", "第 2 行的用例编号 E-0001 重复"),
        ("retrieval/cases.jsonl", "第 3 行的过滤条件不合法：不认识的类型：lesson；可选 defect-pattern、tradeoff、"
                                  "triage-lesson、fix-lesson、contract、reference、issue、finding、fix-report"),
        ("triage/E-0001", "$.title: '' should be non-empty"),
        ("triage/E-0002", "用例编号 E-0009 与目录名 E-0002 不一致"),
        ("triage/E-0004", "excludeItems 中的评分项 triage.unknown 不在 triage 的评分表中"),
    ]


def test_seal_requires_an_interactive_user(world):
    world.module_case("E-0001")
    confirm_calls = []

    def confirm(changes):
        confirm_calls.append(list(changes))
        return True

    with pytest.raises(SealRefused, match="交互终端"):
        manifest.seal(world.layout, interactive=False, environ={}, confirm=confirm, tracer=world.tracer)
    with pytest.raises(SealRefused, match="agent 执行器"):
        manifest.seal(world.layout, interactive=True, environ={"TIGHTREIN_RUN_ID": "R-20261005-030000-fix"},
                      confirm=confirm, tracer=world.tracer)
    with pytest.raises(SealRefused, match="没有确认"):
        manifest.seal(world.layout, interactive=True, environ={}, confirm=lambda changes: False, tracer=world.tracer)
    assert not world.layout.eval_manifest().exists()
    sha = manifest.seal(world.layout, interactive=True, environ={}, confirm=confirm, tracer=world.tracer)
    assert confirm_calls == [[CaseProblem("triage/E-0001", "没有登记在 manifest.json 中")]]
    assert sha == manifest.file_sha256(world.layout.eval_manifest())
    assert manifest.read(world.layout) == manifest.compute(world.layout)
    [event] = events.read(world.layout.events_log(world.clock.now().date()))
    assert (event.operation, event.decision, event.artifact) == ("user_action", "eval-seal", "evals/manifest.json")
    assert manifest.seal(world.layout, interactive=True, environ={}, confirm=confirm, tracer=world.tracer) == sha
    assert len(confirm_calls) == 1


def test_add_case_creates_the_next_case_skeleton(world, tmp_path):
    world.module_case("E-0001")
    source = world.write_json(tmp_path / "triage-P-0050.json", handoff(subject_id="P-0050"))
    with pytest.raises(SealRefused):
        add_case(world.layout, Stage.TRIAGE, source, "abc1234", interactive=False, environ={})
    directory = add_case(world.layout, Stage.TRIAGE, source, "abc1234", interactive=True, environ={})
    assert directory == world.layout.eval_case_dir(Stage.TRIAGE, "E-0002")
    skeleton = json.loads((directory / "case.json").read_text(encoding="utf-8"))
    assert (skeleton["id"], skeleton["source"], skeleton["input"]) == (
        "E-0002", {"runId": "R-20260920-010000-triage", "subjectId": "P-0050", "correction": None},
        {"handoff": "triage-P-0050.json", "commit": "abc1234"})
    assert (directory / "input" / "triage-P-0050.json").read_text(encoding="utf-8") == source.read_text(
        encoding="utf-8")


def run_case(world, issue_id="0007", **changes):
    directory = world.layout.run_cases_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{issue_id}.input.json").write_text(json.dumps(handoff()), encoding="utf-8")
    data = {"schemaVersion": 1, "issueId": issue_id, "title": "订单查询返回 500", "baseCommit": "def5678",
            "mergeCommit": "9" * 40, "input": f"{issue_id}.input.json", **changes}
    (directory / f"{issue_id}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_run_cases_join_the_fix_cases_without_sealing(world):
    world.module_case("E-0001", module="fix", commit="def5678")
    world.sealed()
    run_case(world)
    verified = verify_cases(world.layout, world.process, Stage.FIX)
    assert [case.key for case in verified.module_cases] == ["fix/E-0001", "fix/0007"]
    case = verified.module_cases[1]
    assert (case.commit, case.source["subjectId"], case.input_file.name) == ("def5678", "0007", "0007.input.json")
    assert "fix/0007" in verified.case_hashes
    assert [case.key for case in verify_cases(world.layout, world.process, Stage.TRIAGE).module_cases] == []


def test_malformed_run_cases_are_listed(world):
    world.sealed()
    run_case(world, baseCommit=None)
    with pytest.raises(EvalCaseInvalid) as raised:
        verify_cases(world.layout, world.process, Stage.FIX)
    assert problems(raised) == [("data/eval/cases/0007.json", "缺少 baseCommit")]
