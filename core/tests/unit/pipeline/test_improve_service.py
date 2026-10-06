from datetime import timedelta
from pathlib import Path

from learn_world import make_learn_world, problem_with, triaged
from pipeline_world import NOW

from tightrein.domain.enums import (
    EvalCaseCategory,
    EvalVerdict,
    RunnerStatus,
    ScoreMethod,
    ScoreResult,
    Stage,
    SuggestionKind,
    TriageOutcome,
)
from tightrein.evaluation.cases import ModuleCase
from tightrein.evaluation.report import EvaluationReport
from tightrein.evaluation.scorers.base import ItemResult
from tightrein.evaluation.stats import RunScore
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.pipeline.improve.service import ImproveDeps, ImproveService, patch_problem
from tightrein.runner.result import Usage
from tightrein.store.files import documents
from tightrein.store.repos import suggestions

PATCH = ("--- a/skills/triage/references/evidence-standard.md\n+++ b/skills/triage/references/evidence-standard.md\n"
         "@@ -1 +1 @@\n-旧\n+判不成立前先追到入口\n")
EVAL_ID = "EV-20261005-030000"
MODELS = {name: {"tool": "claude", "model": name, "inputUsdPerMTok": 1, "outputUsdPerMTok": 5}
          for name in ("haiku", "sonnet", "opus")}
ROUTES = {"default": "opus", "triage.refuter": "sonnet", "fix.review.deep": "sonnet"}


def suggestion(target="prompt", patch=PATCH, model=None, addresses=("P-0001",)):
    return {"reason": "三次误判都没有追到入口", "suggestion": {
        "stage": "triage", "target": target, "patch": patch, "model": model,
        "rationale": "取证底线没有要求判不成立前追到入口", "addresses": list(addresses),
        "expected": "参与改进的用例反证检查通过"}}


def case(case_id, subject):
    return ModuleCase(case_id, Stage.TRIAGE, "用例", EvalCaseCategory.REPRESENTATIVE, {"subjectId": subject},
                      "handoff.json", "abc1234", (), {}, (), {}, Path("evals/triage") / case_id)


CASES = [case("E-0001", "P-0001"), case("E-0002", "P-0002"), case("E-0003", "P-0003"), case("E-0004", "P-0009"),
         case("E-0005", "0012")]


def run(case_id, variant, passed):
    result = ScoreResult.PASS if passed else ScoreResult.FAIL
    return RunScore(case_id, variant, 1, RunnerStatus.OK, [ItemResult("triage.counter-check", ScoreMethod.CODE, result, "")],
                    1.0 if passed else 0.0, passed, Usage(), 0, Path("out"))


class Evaluator:
    """baseline 只通过未参与改进的用例；候选的结果由 candidate 给出(用例编号到是否通过)。"""

    def __init__(self, candidate, verdict=EvalVerdict.PASS):
        self.candidate = candidate
        self.verdict = verdict
        self.plans = []

    def __call__(self, plan):
        self.plans.append(plan)
        baseline, candidate = (variant.label for variant in plan.variants[:2])
        held_out = {"E-0004", "E-0005"}
        runs = [run(case_id, baseline, case_id in held_out) for case_id in plan.case_ids]
        runs += [run(case_id, candidate, self.candidate.get(case_id, case_id in held_out)) for case_id in plan.case_ids]
        return EvaluationReport(EVAL_ID, plan, "m", "t", [], [], [], self.verdict, runs)


def make(tmp_path, evaluator, troubles=3, cases=CASES):
    world = make_learn_world(tmp_path, models=MODELS, routes=ROUTES)
    for number in range(1, troubles + 1):
        problem_with(world, f"P-000{number}")
        triaged(world, f"P-000{number}", outcome=TriageOutcome.FALSE_REFUTE, outcome_at=NOW - timedelta(days=1))
    deps = ImproveDeps(world.layout, world.tool, world.config, world.conn, world.clock,
                       EventLog(world.layout, Redactor()), world.runner, lambda stage: cases, evaluator)
    return world, ImproveService(deps)


def test_few_troubles_do_not_call_the_model(tmp_path):
    world, service = make(tmp_path, Evaluator({}), troubles=2)
    result = service.suggest()
    assert result.suggestion_id is None and "只有 2 条" in result.outputs["reason"]
    assert world.runner.tasks == []


def test_an_improvement_that_helps_without_side_effects_is_recommended(tmp_path):
    evaluator = Evaluator({"E-0001": True, "E-0002": True, "E-0003": True})
    world, service = make(tmp_path, evaluator)
    world.runner.add("improvement-writer", suggestion())
    result = service.suggest()
    assert (result.suggestion_id, result.outputs["recommended"]) == ("LS-0001", True)
    (plan,) = evaluator.plans
    assert plan.case_ids == ("E-0001", "E-0002", "E-0003", "E-0004", "E-0005") and plan.variants[1].version.patch is not None
    summary = result.outputs["summary"]
    assert summary["involved"]["baseline"]["deterministic"] == 0.0
    assert summary["involved"]["candidate"]["deterministic"] == 1.0
    assert summary["heldOut"]["candidate"]["deterministic"] == 1.0
    (record,) = suggestions.find(world.conn, kind=SuggestionKind.IMPROVEMENT)
    assert record.target_path == "data/improve/LS-0001.md" and record.diff == PATCH
    assert world.layout.improve_file("LS-0001", ".patch").read_text(encoding="utf-8") == PATCH
    text = world.layout.improve_file("LS-0001", ".md").read_text(encoding="utf-8")
    assert documents.check(text) == []
    document = documents.read(world.layout.improve_file("LS-0001", ".md"))
    assert document.header["kind"] == "decision" and "未参与改进(2 个)" in document.sections["background"]
    assert "实际结果：误判为不成立" in world.runner.tasks[0].instructions.prompt
    assert sorted(path.name for path in world.layout.improve_dir().iterdir()) == ["LS-0001.md", "LS-0001.patch"]


def test_a_duplicate_suggestion_leaves_no_temporary_patch(tmp_path):
    world, service = make(tmp_path, Evaluator({"E-0001": True, "E-0002": True, "E-0003": True}))
    world.runner.add("improvement-writer", suggestion()).add("improvement-writer", suggestion())
    assert service.suggest().suggestion_id == "LS-0001"
    world.clock.advance(timedelta(seconds=1))
    again = service.suggest()
    assert again.suggestion_id is None and again.outputs["reason"] == "同一建议已在等待处理"
    assert sorted(path.name for path in world.layout.improve_dir().iterdir()) == ["LS-0001.md", "LS-0001.patch"]


def test_an_improvement_that_hurts_held_out_cases_is_not_recommended(tmp_path):
    world, service = make(tmp_path, Evaluator({"E-0001": True, "E-0004": False}))
    world.runner.add("improvement-writer", suggestion())
    result = service.suggest()
    assert result.outputs["recommended"] is False
    assert "未参与改进的用例确定性检查通过率下降" in result.outputs["reason"]
    assert suggestions.get(world.conn, "LS-0001").evidence["advice"]["recommendation"] == "拒绝"


def test_too_few_held_out_cases_or_patches_outside_skills_give_no_suggestion(tmp_path):
    world, service = make(tmp_path, Evaluator({}), cases=CASES[:3])
    world.runner.add("improvement-writer", suggestion())
    assert "未参与改进的只有 0 个" in service.suggest().outputs["reason"]
    evaluator = Evaluator({})
    world, service = make(tmp_path / "other", evaluator)
    world.runner.add("improvement-writer", suggestion(patch=PATCH.replace("skills/triage", "core/tightrein")))
    assert service.suggest().outputs["reason"].startswith("补丁只能改 skills/")
    assert evaluator.plans == [] and suggestions.find(world.conn) == []


def test_no_common_cause_gives_no_suggestion(tmp_path):
    world, service = make(tmp_path, Evaluator({}))
    world.runner.add("improvement-writer", {"suggestion": None, "reason": "三次误判原因各不相同"})
    assert service.suggest().outputs["reason"] == "没有可归纳的规律：三次误判原因各不相同"


def test_patches_may_not_touch_evaluation_or_guards():
    assert patch_problem(None) == "补丁为空或不是统一格式"
    assert patch_problem(PATCH) is None
    assert patch_problem("+++ b/skills/improve/x.md\n") == "补丁只能改 skills/ 下的角色说明与参考资料：skills/improve/x.md"


def test_a_model_suggestion_compares_the_alias_with_the_current_route(tmp_path):
    evaluator = Evaluator({"E-0001": True, "E-0002": True, "E-0003": True})
    world, service = make(tmp_path, evaluator)
    world.runner.add("improvement-writer", suggestion(target="model", patch=None, model="sonnet"))
    result = service.suggest()
    assert result.suggestion_id == "LS-0001"
    (plan,) = evaluator.plans
    assert [(variant.runner, variant.model) for variant in plan.variants] == [("claude", "opus"), ("claude", "sonnet")]
    assert "- sonnet：工具 claude，模型 sonnet" in world.runner.tasks[0].instructions.prompt
    assert "`triage.claim-verifier: sonnet`" in world.layout.improve_file("LS-0001", ".md").read_text(encoding="utf-8")
