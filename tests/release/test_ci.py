"""等 CI：按每项的 bucket 判断(退出码不算)；没有检查为 skipped；等到时限仍没结束的按进行中返回；
要求必需检查时只等必需检查。"""

import json

from tightrein.protocol.git.github import Check
from tightrein.protocol.process import Outcome
from tightrein.release import ci


def _outcome(code, stdout="", stderr="", stopped=None):
    return Outcome(code, stdout, stderr, 1, stopped, None)


def test_buckets_decide_the_state():
    assert ci.judge([]).state == ci.SKIPPED
    assert ci.judge([{"name": "a", "bucket": "pass"}, {"name": "b", "bucket": "skipping"}]).state == ci.PASSED
    pending = ci.judge([{"name": "a", "bucket": "pass"}, {"name": "b", "bucket": "pending"}])
    assert pending.state == ci.PENDING and not pending.finished and "b" in (pending.reason or "")
    failed = ci.judge([{"name": "a", "bucket": "cancel"}, {"name": "b", "bucket": "pending"}])
    assert failed.state == ci.FAILED and failed.failed == ("a",) and failed.pending == ("b",)
    assert failed.reason == "CI 检查未通过：a"


def test_waiting_watches_then_reads_the_result(github_runtime, fake_github):
    runner = fake_github.runner
    runner.replies[("gh", "pr", "checks", "186", "--watch")] = [_outcome(1)]  # 退出码不反映结果
    runner.replies[("gh", "pr", "checks", "186", "--json")] = [
        _outcome(1, json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass"}]))]
    state = ci.wait(github_runtime, fake_github, 186, required=False)
    assert state.state == ci.PASSED
    watch = next(command for command in runner.commands if "--watch" in command.argv)
    assert watch.argv[-2:] == ("--repo", "cty/sample") and "--interval" in watch.argv
    assert watch.timeout_s == github_runtime.settings.duration("limits.timeouts.ci")


def test_a_watch_that_times_out_returns_the_current_state(github_runtime, fake_github):
    runner = fake_github.runner
    runner.replies[("gh", "pr", "checks", "186", "--watch")] = [_outcome(None, stopped="timeout")]
    runner.replies[("gh", "pr", "checks", "186", "--json")] = [
        _outcome(8, json.dumps([{"name": "test", "state": "PENDING", "bucket": "pending"}]))]
    assert ci.wait(github_runtime, fake_github, 186, required=False).state == ci.PENDING


def test_no_checks_at_all_is_skipped(github_runtime, fake_github):
    fake_github.runner.replies[("gh", "pr", "checks", "186", "--json")] = [
        _outcome(1, "", "no checks reported on the 'fix/7' branch")]
    assert ci.current(github_runtime, fake_github, 186, required=False).state == ci.SKIPPED


def test_required_checks_come_from_github(github_runtime, fake_github):
    fake_github.checks = [Check("build", "FAILURE", "fail")]
    assert ci.current(github_runtime, fake_github, 186, required=True).failed == ("build",)
