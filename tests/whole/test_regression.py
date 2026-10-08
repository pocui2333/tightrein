"""整体测试的回归路径：第一次修复合并后探针仍报 → 回归、撤销 PR、退回待修 → 第二次修复 → 验收通过。

与 test_pipeline 同一个示例项目与命令行入口，录制集为 tests/fixtures/recordings_regression/(answers/regression/
优先)：第一次编码只处理了 None(average([]) 仍除以零)，合并后探针在线上版本上照报，验收判为回归；第二次编码改为
空列表也返回 0。断言第二次修复真正从头做了一遍(交接只看这一次的、上一次的归档在 attempt_1/)，发布提了新的 PR 而
不是复用已合并的那个，验收的观察期从这一次的部署算起。
"""

import json
from pathlib import Path

import pytest

from fixtures import sample

ISSUE = "0001"
IMPLEMENT_STEPS = ("31-implement.prepare", "33-implement.design", "34-implement.approve", "35-implement.code.r1",
                   "36-implement.check.r1", "37-implement.review.r1", "38-implement.deliver.r1")
RELEASE_STEPS = ("41-release.pr", "42-release.ci", "43-release.merge", "44-release.deploy", "45-release.accept")
RECORDED_POINTS = ["assess.triage", "implement.design", "implement.code", "implement.review", "retro.idea",
                   "implement.design", "implement.code", "implement.review"]


@pytest.fixture(scope="module")
def finished(tmp_path_factory: pytest.TempPathFactory) -> tuple[sample.Harness, list, list]:
    with sample.offline() as attempts:
        h = sample.harness(tmp_path_factory.mktemp("regression"), scenario=sample.REGRESSION)
        results = sample.drive(h)
    return h, results, attempts


def _handoff(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _issue(h: sample.Harness) -> dict:
    return json.loads((h.sample.layout.data_dir / "issues" / ISSUE / "00-issue-record.json").read_text(encoding="utf-8"))


def test_the_regressed_issue_is_fixed_by_a_second_attempt(finished: tuple[sample.Harness, list, list]) -> None:
    h, results, _ = finished
    assert sample.issue_statuses(h.sample) == {ISSUE: "done"}
    failed = [(index, step["stage"], step["summary"]) for index, result in enumerate(results, 1)
              for step in result.data["result"]["steps"] if step["status"] != "passed"]
    assert [(index, stage) for index, stage, _ in failed] == [(2, "release")]
    assert "回归" in failed[0][2] and "撤销 PR" in failed[0][2]
    record = _issue(h)
    assert record["extra"]["attempt"] == 2
    (first,) = record["extra"]["attempts"]
    assert (first["attempt"], first["pr"], first["reason"]) == (1, 1, "regress")
    assert first["release"]["revert"]["pr"] == 2 and first["mergeCommit"]
    assert record["pr"] == 3 and record["mergeCommit"] != first["mergeCommit"]


def test_the_second_attempt_starts_over_and_the_first_is_archived(finished: tuple[sample.Harness, list, list]) -> None:
    h, _, _ = finished
    directory = h.sample.layout.data_dir / "issues" / ISSUE
    archived = directory / "attempt_1"
    for step in (*IMPLEMENT_STEPS, *RELEASE_STEPS):
        assert (archived / f"{step}-handoff.json").is_file(), step
        assert (directory / f"{step}-handoff.json").is_file(), step
    first_accept = _handoff(archived / "45-release.accept-handoff.json")
    assert first_accept["status"] == "failed" and first_accept["facts"]["result"] == "regressed"
    statuses = {step: _handoff(directory / f"{step}-handoff.json")["status"] for step in (*IMPLEMENT_STEPS,
                                                                                         *RELEASE_STEPS)}
    assert set(statuses.values()) == {"passed"}, statuses
    # 第二次在新的修复目录里从准备做起，不沿用第一次「已完成」的各步
    prepare = _handoff(directory / "31-implement.prepare-handoff.json")
    assert prepare["facts"]["worktree"].endswith("worktrees/0001_2")
    first_prepare = _handoff(archived / "31-implement.prepare-handoff.json")
    assert prepare["run"] != first_prepare["run"]
    # 回归时写的说明(含撤销 PR)随第一次归档；交付文档只有第二次的
    assert "pull/2" in (archived / "90-issue-failure.md").read_text(encoding="utf-8")
    assert not (directory / "90-issue-failure.md").exists() and (directory / "90-issue-deliver.md").is_file()


def test_release_opens_a_new_pull_and_accepts_from_the_new_deploy(finished: tuple[sample.Harness, list, list]) -> None:
    h, _, _ = finished
    pulls = {number: (pull.state, pull.head_ref.startswith("hotfix/")) for number, pull in h.github.pulls.items()}
    assert pulls == {1: ("MERGED", False), 2: ("OPEN", True), 3: ("MERGED", False)}
    calc = sample.git(h.sample.origin, "show", "main:calc.py")
    assert "if not values:" in calc and "values is None" not in calc
    accept = _handoff(h.sample.layout.data_dir / "issues" / ISSUE / "45-release.accept-handoff.json")
    assert accept["facts"]["result"] == "passed"
    # 观察期从第二次的部署算起：第一次回归时的那次出现不再算
    deploy = _handoff(h.sample.layout.data_dir / "issues" / ISSUE / "44-release.deploy-handoff.json")
    first_deploy = _handoff(h.sample.layout.data_dir / "issues" / ISSUE / "attempt_1" / "44-release.deploy-handoff.json")
    assert deploy["facts"]["at"] > first_deploy["facts"]["at"]
    # 部署后才能确认的验收标准由验收按指纹确认
    assert [item["result"] for item in accept["facts"]["criteria"]] == ["passed"]
    assert "部署后的观察期" in accept["facts"]["criteria"][0]["criterion"]


def test_offline_and_replayed(finished: tuple[sample.Harness, list, list]) -> None:
    h, _, attempts = finished
    assert attempts == [] and h.runner.refused == [] and h.github.unknown == []
    assert [point for point, _, _ in h.replay.calls] == RECORDED_POINTS
    index = json.loads((sample.SCENARIOS[sample.REGRESSION].recordings / "index.json").read_text(encoding="utf-8"))
    assert [entry["point"] for entry in index["recordings"]] == RECORDED_POINTS
    assert sorted(Path(h.sample.layout.data_dir).rglob("*-raw.jsonl")) == []
