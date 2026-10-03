import json
from pathlib import Path

from tightrein.contracts import validate
from tightrein.domain.enums import EvalVerdict, RunnerStatus, ScoreMethod, ScoreResult, Stage
from tightrein.evaluation import compare, report, stats
from tightrein.evaluation.report import EvaluationReport
from tightrein.evaluation.scorers.base import ItemResult
from tightrein.evaluation.scoring import summarize
from tightrein.evaluation.stats import RunScore
from tightrein.evaluation.variants import VersionSpec, tool_model_plan, version_plan
from tightrein.runner.result import Usage
from tightrein.store.files.layout import WorkspaceLayout

PLAN = version_plan(Stage.TRIAGE, VersionSpec("IP-0003", patch=Path("proposals/IP-0003.patch")), "claude",
                    "claude-opus", ("E-0001", "E-0002"))
TRANSCRIPT = "transcripts/claim-verifier-P-0042.jsonl"


def run(case_id, variant, attempt, *results):
    items = [ItemResult(f"triage.item-{index}", ScoreMethod.CODE, ScoreResult(value),
                        "评审无法判断" if value == "unknown" else "") for index, value in enumerate(results)]
    score, passed = summarize(items)
    return RunScore(case_id, variant, attempt, RunnerStatus.OK, items, score, passed, Usage(100, 10, None, 0.5), 1200,
                    Path("/ev/outputs") / variant / case_id / str(attempt), None, (TRANSCRIPT,))


def build(plan, runs, same_model=()):
    labels = [variant.label for variant in plan.variants]
    case_stats = stats.case_stats(runs, labels, list(plan.case_ids))
    comparisons = compare.compare(case_stats, labels[0])
    return EvaluationReport("EV-20261005-030000", plan, "a" * 64, "b" * 40, case_stats,
                            stats.variant_stats(runs, case_stats, labels), comparisons,
                            compare.verdict(plan, comparisons, runs, True), runs, [], list(same_model))


def version_runs():
    runs = [run("E-0001", "baseline", number, "pass", "pass") for number in (1, 2, 3)]
    runs += [run("E-0001", "IP-0003", 1, "pass", "fail"), run("E-0001", "IP-0003", 2, "pass", "pass"),
             run("E-0001", "IP-0003", 3, "pass", "pass")]
    runs += [run("E-0002", "baseline", number, "pass", "pass") for number in (1, 2, 3)]
    runs += [run("E-0002", "IP-0003", 1, "pass", "unknown"), run("E-0002", "IP-0003", 2, "pass", "pass"),
             run("E-0002", "IP-0003", 3, "pass", "pass")]
    return runs


EXPECTED = """# 评测报告 EV-20261005-030000

## 结论

- 判定：否决(reject)
- 变差的用例：1 个(E-0001)
- 需要人工判断的项：1 项

## 评测对象

- 模块：triage
- 每个用例每个变体运行 3 次，共 2 个用例
- 用例集哈希：aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
- evals/ 树对象：bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

| 变体 | 版本 | commit | 补丁 | 执行器 | 模型 | 评审同模型 |
|---|---|---|---|---|---|---|
| baseline | baseline | HEAD | 无 | claude | claude-opus | 否 |
| IP-0003 | IP-0003 | HEAD | proposals/IP-0003.patch | claude | claude-opus | 是 |

## 总表

| 变体 | 总体均值 | 通过率 | 与基线的差值 | 总费用(USD) | 平均耗时(ms) |
|---|---|---|---|---|---|
| baseline | 1.000 | 1.000 | — | 3.00 | 1200 |
| IP-0003 | 0.917 | 0.667 | -0.083 | 3.00 | 1200 |

## 变差的用例

### E-0001(IP-0003)

- 基线均值 1.000，方差 0.000；候选均值 0.833，方差 0.083；差值 -0.167
- triage.item-1：通过 3/3 → 2/3
- 输出目录：/ev/outputs/IP-0003/E-0001/1

## 逐用例

| 用例 | 变体 | 均值 | 方差 | 通过率 | 不稳定 |
|---|---|---|---|---|---|
| E-0001 | baseline | 1.000 | 0.000 | 1.000 | 否 |
| E-0001 | IP-0003 | 0.833 | 0.083 | 0.667 | 是 |
| E-0002 | baseline | 1.000 | 0.000 | 1.000 | 否 |
| E-0002 | IP-0003 | 1.000 | 0.000 | 0.667 | 是 |

## 逐评分项

| 评分项 | baseline | IP-0003 |
|---|---|---|
| triage.item-1 | 6/6 | 4/6 |
| triage.item-0 | 6/6 | 6/6 |

## 需要人工判断

- IP-0003 / E-0002 / 第 1 次 / triage.item-1：评审无法判断

## 失败的运行

- IP-0003 / E-0001 / 第 1 次：评分项未全部通过；输出目录 /ev/outputs/IP-0003/E-0001/1；会话记录 /ev/outputs/IP-0003/E-0001/1/transcripts/claim-verifier-P-0042.jsonl
- IP-0003 / E-0002 / 第 1 次：评分项未全部通过；输出目录 /ev/outputs/IP-0003/E-0002/1；会话记录 /ev/outputs/IP-0003/E-0002/1/transcripts/claim-verifier-P-0042.jsonl
"""


def test_the_markdown_report_matches_the_expected_text():
    built = build(PLAN, version_runs(), ["IP-0003"])
    assert built.verdict is EvalVerdict.REJECT
    assert report.render_markdown(built) == EXPECTED


def test_the_json_report_follows_the_schema(tmp_path):
    layout = WorkspaceLayout(tmp_path)
    built = build(PLAN, version_runs())
    path = report.write(layout, built)
    assert path == layout.eval_report_md("EV-20261005-030000") and path.is_file()
    data = json.loads(layout.eval_report_json("EV-20261005-030000").read_text(encoding="utf-8"))
    assert validate.validate("data/eval-report.schema.json", data) == []
    assert (data["verdict"], data["plan"]["purpose"], len(data["caseStats"]), len(data["comparisons"])) == (
        "reject", "version", 4, 2)
    assert data["comparisons"][0]["itemChanges"] == [{"itemId": "triage.item-1", "baseline": [3, 3],
                                                      "candidate": [2, 3]}]


def test_tool_and_model_reports_rank_variants_without_a_verdict():
    plan = tool_model_plan(Stage.TRIAGE, ["claude", "codex"], case_ids=("E-0001",))
    runs = [run("E-0001", "claude+default", number, "pass", "fail") for number in (1, 2, 3)]
    runs += [run("E-0001", "codex+default", number, "pass", "pass") for number in (1, 2, 3)]
    built = build(plan, runs)
    assert built.to_dict()["verdict"] is None
    text = report.render_markdown(built)
    assert "- 判定：不做否决判定(工具与模型对比)" in text
    totals = text.split("## 总表")[1]
    assert totals.index("codex+default") < totals.index("claude+default")
    assert "## 变差的用例\n\n无\n" in text
