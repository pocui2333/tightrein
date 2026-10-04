"""Plan 38 外部做法借鉴单元测试(1-7 项)。"""

from unittest.mock import MagicMock

import pytest

from tightrein.domain.enums import RegressionKind, RegressionResult, ReviewMode
from tightrein.pipeline.checks.output_trim import trim
from tightrein.pipeline.checks.regressions.manifest import CheckEntry
from tightrein.pipeline.checks.regressions.repo_test_check import FailureKind, RepoTestCheck, classify_failure
from tightrein.pipeline.fix.prompts import fix_reviewer
from tightrein.pipeline.fix.steps import checkpoint, plan


# 1. 复现测试防假红
PATTERNS = {"testBroken": ["SyntaxError:", "NameError:"], "environment": ["Connection refused"]}


@pytest.mark.parametrize(("text", "kind"), [
    ("SyntaxError: invalid syntax\n", FailureKind.TEST_BROKEN),
    ("ConnectionRefusedError: [Errno 61] Connection refused\n", FailureKind.ENVIRONMENT),
    ("AssertionError: 1 != 2\n", FailureKind.ASSERTION),
])
def test_classify_failure(text, kind):
    assert classify_failure(text, PATTERNS) is kind


def _checker(outputs, exit_codes, retries=1):
    calls = []

    def launcher(command):
        index = len(calls)
        calls.append(command)
        command.log_file.write_text(outputs[index], encoding="utf-8")
        return MagicMock(started=True, timed_out=False, exit_code=exit_codes[index])

    return RepoTestCheck(launcher, {}, 30, lambda cmd, file: ".", (1,), failure_patterns=PATTERNS,
                         env_retries=retries), calls


def _entry(tmp_path, signature=None):
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    (worktree / "test_sample.py").write_text("# test\n")
    return CheckEntry("C-1", RegressionKind.TEST, "test_sample.py", "test_sample.py:1", command="pytest test_sample.py",
                      expected_signature=signature), worktree


def test_environment_failure_is_retried_then_passes(tmp_path):
    checker, calls = _checker(["Connection refused\n", "ok\n"], [1, 0])
    entry, worktree = _entry(tmp_path)
    assert checker(entry, tmp_path / "r", worktree, tmp_path / "raw").result is RegressionResult.PASSED
    assert len(calls) == 2


def test_environment_failure_after_retries_stops_the_base_run(tmp_path):
    checker, calls = _checker(["Connection refused\n"] * 2, [1, 1])
    entry, worktree = _entry(tmp_path)
    run = checker.base(entry, tmp_path / "r", worktree, tmp_path / "raw")
    assert run.environment and run.execution.result is RegressionResult.NOT_RUN and len(calls) == 2


def test_assertion_failure_is_not_retried(tmp_path):
    checker, calls = _checker(["AssertionError\n"], [1])
    entry, worktree = _entry(tmp_path)
    assert checker(entry, tmp_path / "r", worktree, tmp_path / "raw").result is RegressionResult.FAILED
    assert len(calls) == 1


def test_broken_test_is_invalid_only_on_the_base_run(tmp_path):
    entry, worktree = _entry(tmp_path)
    checker, _ = _checker(["NameError: name 'x' is not defined\n"], [1])
    assert checker.base(entry, tmp_path / "r", worktree, tmp_path / "raw").execution.result is RegressionResult.INVALID
    checker, _ = _checker(["NameError: name 'x' is not defined\n"], [1])
    assert checker(entry, tmp_path / "r", worktree, tmp_path / "raw").result is RegressionResult.FAILED


@pytest.mark.parametrize(("output", "result"), [
    ("KeyError: 'group_key'\n", RegressionResult.FAILED),
    ("NameError: KeyError: 'group_key'\n", RegressionResult.FAILED),
    ("AssertionError: 1 != 2\n", RegressionResult.INVALID),
])
def test_expected_signature_decides_the_base_run(tmp_path, output, result):
    checker, _ = _checker([output], [1])
    entry, worktree = _entry(tmp_path, "KeyError: 'group_key'")
    assert checker.base(entry, tmp_path / "r", worktree, tmp_path / "raw").execution.result is result


def test_expected_signature_round_trips_through_the_manifest(tmp_path):
    entry, _ = _entry(tmp_path, "KeyError: 'group_key'")
    assert CheckEntry.from_dict(entry.to_dict()).expected_signature == "KeyError: 'group_key'"


# 2. 测试输出精简
def test_output_trim_preserves_failures_and_drops_passes():
    raw = (
        "test_a.py::test_pass1 PASSED\n"
        "test_a.py::test_pass2 PASSED\n"
        "FAIL: test_b (tests.test_b)\n"
        "Traceback (most recent call last):\n"
        "  File \"test_b.py\", line 10, in test_b\n"
        "    assert False\n"
        "AssertionError\n"
        "=================== 1 failed, 2 passed in 0.12s ===================\n"
    )
    trimmed = trim(raw)
    assert "AssertionError" in trimmed
    assert "FAIL: test_b" in trimmed
    assert "test_pass1 PASSED" not in trimmed and "test_pass2 PASSED" not in trimmed


def test_output_trim_keeps_unrecognized_output_and_its_tail():
    assert trim("something odd\nhappened") == "something odd\nhappened"
    trimmed = trim("x" * 50 + "END", max_chars=10)
    assert trimmed.endswith("xxxxxxxEND")


# 3. 最佳状态检查点
def _checkpoint(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    git = MagicMock()
    git.show.side_effect = lambda repo, rev, path: {"src/a.py": "base a\n", "src/gone.py": "base gone\n"}.get(path)
    return checkpoint.Checkpoint(git, worktree, "base123", tmp_path / "fix" / "checkpoint"), worktree


def test_checkpoint_restores_saved_files_and_reverts_later_changes(tmp_path):
    point, worktree = _checkpoint(tmp_path)
    (worktree / "src/a.py").write_text("good a\n")
    point.save(1, ["src/a.py", "src/gone.py"], 0)
    (worktree / "src/a.py").write_text("bad a\n")
    (worktree / "src/b.py").write_text("new later\n")
    (worktree / "src/gone.py").write_text("recreated\n")
    note = point.rollback(["src/a.py", "src/b.py", "src/gone.py"], "第 2 轮复现检查重新失败")
    assert (worktree / "src/a.py").read_text() == "good a\n"
    assert not (worktree / "src/b.py").exists()
    assert not (worktree / "src/gone.py").exists()
    assert "第 1 轮" in note and "复现检查重新失败" in note


def test_checkpoint_after_later_edit_to_an_unsaved_file_restores_base(tmp_path):
    point, worktree = _checkpoint(tmp_path)
    point.save(1, [], 0)
    (worktree / "src/a.py").write_text("bad a\n")
    point.rollback(["src/a.py"], "x")
    assert (worktree / "src/a.py").read_text() == "base a\n"


def test_checkpoint_decisions_and_persistence(tmp_path):
    point, worktree = _checkpoint(tmp_path)
    assert not point.should_save(False, 0) and point.should_save(True, 2)
    point.save(1, [], 2)
    assert not point.should_save(True, 3) and point.should_save(True, 1)
    assert point.should_rollback(False, 3, 5) and point.should_rollback(True, 5, 5)
    assert not point.should_rollback(True, 4, 5) and not point.should_rollback(False, 2, 5)
    reloaded = checkpoint.Checkpoint(MagicMock(), worktree, "base123", tmp_path / "fix" / "checkpoint")
    assert reloaded.exists
    assert not checkpoint.Checkpoint(MagicMock(), worktree, "other", tmp_path / "fix" / "checkpoint").exists
    reloaded.clear()
    assert not reloaded.exists


def test_checkpoint_rejects_paths_outside_the_worktree(tmp_path):
    point, _ = _checkpoint(tmp_path)
    with pytest.raises(ValueError):
        point.save(1, ["../escape.py"], 0)


# 4. 计划每步必须对应验收标准
def test_plan_check_requires_all_steps_mapped_to_acceptance(tmp_path):
    context = MagicMock()
    context.acceptance = ["只显示当天订单"]
    plan_data = {
        "files": [], "flags": {"design": {"flagged": False}}, "estimate": {"files": 1, "lines": 5},
        "steps": [
            {"file": "a.py", "change": "步骤1"},
            {"file": "b.py", "change": "步骤2未映射"},
        ],
        "protectedTouches": [],
        "acceptanceMapping": [{"criterion": "只显示当天订单", "steps": [1]}],
        "notDoing": ["不修改其它文件"],
        "hypothesis": {"cause": "只按日期范围查询", "evidence": [], "edits": []},
    }
    checked = plan.check(plan_data, context, tmp_path, (), 10, 100, ())
    assert any("第 2 步" in p and "没有对应任何验收标准" in p for p in checked.problems)


# 5. 深度评审盲审
def test_deep_review_blind_prompt(tmp_path):
    prompt_tool = MagicMock()
    prompt_tool.role.return_value = "# fix-reviewer 角色"
    prompt_tool.config = MagicMock()
    ctx = MagicMock()
    ctx.issue_id = "0042"
    ctx.acceptance = ["订单过滤正确"]
    task = fix_reviewer.task(prompt_tool, ctx, {"summary": "plan summary"}, "diff --git a b", [],
                             ReviewMode.DEEP, 1, results="复现测试通过")
    prompt_text = task.instructions.prompt
    assert "盲审" in prompt_text
    assert "验收标准" in prompt_text
    assert "最终 diff" in prompt_text
    assert "实际结果(第 7 步)" in prompt_text
    # 不提供计划 summary
    assert "plan summary" not in prompt_text
