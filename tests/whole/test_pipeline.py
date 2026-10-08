"""整体测试：示例项目 demo-app 从采集跑到发布，经命令行(`tightrein run`)按调度一轮轮推进。

采集(项目探针真跑 average([]) 发现崩溃) → 去重成问题 → 评估取证(回放)写成 Issue(低风险自动放行) → 实施：准备
worktree、定位(评估的代码笔记已有核心位置，跳过)、方案(回放)、定案(自动)、编码(回放，录制的 changes.patch 改代码)、
自检(真跑示例项目的 pytest)、审查(回放)、交付 → 发布：提交、推送到本地裸远程、假 GitHub 的 PR 与 CI、合并 →
验收(没有部署来源：合并时间加 assumeDeployedAfter，再加观察期；FixedClock 拨快，探针在线上版本上不再报) → 复盘。

不联网(本进程的 TCP 连接与联网程序都被拦下)、不调用真实模型(全部模型调用回放 tests/fixtures/recordings/)。
录制与流程对不上时按 tests/README.md「重新录制」处理。
"""

import json
from pathlib import Path

import pytest

from fixtures import sample

ISSUE = "0001"
PROBLEM = "P-0001"
# 每一步落盘的交接(相对工作区 data/)：问题与 Issue 的各步骤
OBJECT_HANDOFFS = (
    f"problems/{PROBLEM}/19-collect.dedup-handoff.json",
    f"problems/{PROBLEM}/21-assess.triage-handoff.json",
    f"issues/{ISSUE}/22-assess.issue-handoff.json",
    f"issues/{ISSUE}/31-implement.prepare-handoff.json",
    f"issues/{ISSUE}/32-implement.locate-handoff.json",
    f"issues/{ISSUE}/33-implement.design-handoff.json",
    f"issues/{ISSUE}/34-implement.approve-handoff.json",
    f"issues/{ISSUE}/35-implement.code.r1-handoff.json",
    f"issues/{ISSUE}/36-implement.check.r1-handoff.json",
    f"issues/{ISSUE}/37-implement.review.r1-handoff.json",
    f"issues/{ISSUE}/38-implement.deliver.r1-handoff.json",
    f"issues/{ISSUE}/41-release.pr-handoff.json",
    f"issues/{ISSUE}/42-release.ci-handoff.json",
    f"issues/{ISSUE}/43-release.merge-handoff.json",
    f"issues/{ISSUE}/44-release.deploy-handoff.json",
    f"issues/{ISSUE}/45-release.accept-handoff.json",
    f"issues/{ISSUE}/46-release.cleanup-handoff.json",
)
# 每次运行都落盘的交接(相对 data/runs/<运行>/)
RUN_HANDOFFS = ("11-collect.project_probes-handoff.json", "17-collect.incidental-handoff.json",
                "19-collect.dedup-handoff.json", "51-retro.detect-handoff.json")
RECORDED_POINTS = ["assess.triage", "implement.design", "implement.code", "implement.review"]


@pytest.fixture(scope="module")
def finished(tmp_path_factory: pytest.TempPathFactory) -> tuple[sample.Harness, list, list]:
    """跑一遍整体流程(模块内只跑一次，各断言共用结果)。"""
    with sample.offline() as attempts:
        h = sample.harness(tmp_path_factory.mktemp("whole"))
        results = sample.drive(h)
    return h, results, attempts


def test_issue_done_after_runs(finished: tuple[sample.Harness, list, list]) -> None:
    h, results, _ = finished
    failures = [(index, step) for index, result in enumerate(results, 1)
                for step in result.data["result"]["steps"] if step["status"] != "passed"]
    assert failures == []
    assert [result.code for result in results] == [0] * len(results)
    assert sample.issue_statuses(h.sample) == {ISSUE: "done"}
    first = [step["stage"] for step in results[0].data["result"]["steps"]]
    assert first[:3] == ["collect", "assess", "implement"] and first[-2:] == ["release", "retro"]
    assert len(results) > 1  # 合并之后靠拨快时钟走完观察期，不在合并那一次运行里完成


def test_every_step_writes_handoff(finished: tuple[sample.Harness, list, list]) -> None:
    h, results, _ = finished
    data = h.sample.layout.data_dir
    missing = [path for path in OBJECT_HANDOFFS if not (data / path).is_file()]
    for result in results:
        run = data / "runs" / result.data["result"]["run"]
        missing += [str(run / name) for name in RUN_HANDOFFS if not (run / name).is_file()]
    assert missing == []
    statuses = {path: json.loads((data / path).read_text(encoding="utf-8"))["status"] for path in OBJECT_HANDOFFS}
    assert set(statuses.values()) == {"passed"}, statuses
    locate = json.loads((data / OBJECT_HANDOFFS[4]).read_text(encoding="utf-8"))
    assert locate["facts"]["skipped"]  # 评估的代码笔记已有核心位置：按已有信息跳过定位
    deliver = data / "issues" / ISSUE / "90-issue-deliver.md"
    assert deliver.is_file() and "average" in deliver.read_text(encoding="utf-8")


def test_the_post_deploy_criterion_is_left_to_acceptance(finished: tuple[sample.Harness, list, list]) -> None:
    """观察类来源的「部署后的观察期内不再出现…」是验收阶段的标准：方案不对应、审查不判断，验收按它确认。"""
    h, _, _ = finished
    data = h.sample.layout.data_dir
    body = (data / "issues" / ISSUE / "00-issue-body.md").read_text(encoding="utf-8")
    assert "部署后的观察期" in body
    review = json.loads((data / OBJECT_HANDOFFS[9]).read_text(encoding="utf-8"))
    assert review["facts"]["acceptance"] and all("部署后" not in item["criterion"]
                                                 for item in review["facts"]["acceptance"])
    accept = json.loads((data / OBJECT_HANDOFFS[15]).read_text(encoding="utf-8"))
    assert [(item["result"], "部署后的观察期" in item["criterion"]) for item in accept["facts"]["criteria"]] == [
        ("passed", True)]


def test_fix_reaches_remote_main(finished: tuple[sample.Harness, list, list]) -> None:
    h, _, _ = finished
    calc = sample.git(h.sample.origin, "show", "main:calc.py")
    assert "if not values:" in calc
    assert "test_average_empty" in sample.git(h.sample.origin, "show", "main:tests/test_calc.py")
    pulls = list(h.github.pulls.values())
    assert [(pull.state, pull.base) for pull in pulls] == [("MERGED", "main")]
    # 自检真的在 worktree 里跑了示例项目的 pytest
    assert any("pytest" in argv for argv in h.runner.commands)


def test_offline_and_replayed(finished: tuple[sample.Harness, list, list]) -> None:
    h, _, attempts = finished
    assert attempts == []  # 本进程没有 TCP 连接
    assert h.runner.refused == []  # 没有启动联网程序或真实的 agent 工具
    assert h.github.unknown == []
    assert [point for point, _, _ in h.replay.calls] == RECORDED_POINTS
    index = json.loads((sample.RECORDINGS / "index.json").read_text(encoding="utf-8"))
    assert [entry["point"] for entry in index["recordings"]] == RECORDED_POINTS
    raws = sorted(Path(h.sample.layout.data_dir).rglob("*-raw.jsonl"))
    assert raws == []  # 回放全部成功：失败的调用才保存原始输出
