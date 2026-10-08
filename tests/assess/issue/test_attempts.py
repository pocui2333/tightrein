"""修复尝试：回归、重开且上一次已动过手时开下一次尝试；上一次的分支、PR、合并提交与发布进度留档后清空；上一次实施与
发布的文件在开始实施前挪进 attempt_<次>/，共用文件与采集、评估的文件留在原处，可重复做。"""

from dataclasses import replace

from tightrein.assess.issue import attempts
from tightrein.assess.issue.transitions import IssueEvent, transition
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables.issues import Issue

RELEASED = {"release": {"pr_url": "https://github.com/o/r/pull/1", "revert": {"pr": 2}}, "acceptUntil": "x",
            "history": []}


def record(status: str = "accepting", **fields) -> Issue:
    base = Issue(id="0007", status=status, title="t", kind="bug", origin="problem",
                 stage="release" if status == "accepting" else None, branch="fix/7-t", pr=1, merge_commit="m" * 40,
                 deploy="m" * 40, extra=dict(RELEASED))
    return replace(base, **fields)


def test_a_regression_after_merging_begins_the_next_attempt(clock):
    after = transition(record(), IssueEvent.REGRESS, reason=None, clock=clock)
    assert after.status == "todo" and attempts.current(after) == 2
    assert (after.branch, after.pr, after.merge_commit, after.deploy) == (None, None, None, None)
    assert "release" not in after.extra and "acceptUntil" not in after.extra
    (first,) = after.extra["attempts"]
    assert (first["attempt"], first["reason"], first["pr"], first["branch"]) == (1, "regress", 1, "fix/7-t")
    assert first["release"]["revert"] == {"pr": 2} and first["mergeCommit"] == "m" * 40
    again = transition(transition(replace(after, status="accepting", stage="release", pr=3, merge_commit="n" * 40),
                                  IssueEvent.REGRESS, reason=None, clock=clock), IssueEvent.START, reason=None,
                       clock=clock)
    assert attempts.current(again) == 3 and [item["pr"] for item in again.extra["attempts"]] == [1, 3]


def test_reopening_counts_only_when_the_last_attempt_did_something(clock):
    done = record("done", extra={"closeReason": "fixed", **RELEASED})
    assert attempts.current(transition(done, IssueEvent.REOPEN, reason=None, clock=clock)) == 2
    untouched = Issue(id="0008", status="cancelled", title="t", kind="bug", origin="problem",
                      extra={"closeReason": "wont_fix"})
    reopened = transition(untouched, IssueEvent.REOPEN, reason=None, clock=clock)
    assert attempts.current(reopened) == 1 and "attempts" not in reopened.extra


def test_leftovers_of_the_last_attempt_are_archived_once(tmp_path):
    layout = WorkspaceLayout(tmp_path)
    directory = layout.subject_dir("0007")
    directory.mkdir(parents=True)
    kept = ["00-issue-body.md", "00-issue-record.json", "21-assess.triage-handoff.json", "22-assess.issue-handoff.json"]
    moved = ["31-implement.prepare-handoff.json", "35-implement.code.r1-handoff.json", "45-release.accept-handoff.json",
             "90-issue-failure.md"]
    for name in kept + moved:
        (directory / name).write_text("{}", encoding="utf-8")
    (directory / "checkpoint").mkdir()
    (directory / "36-implement.check.r1-raw").mkdir()
    first = record("todo")
    assert attempts.archive(layout, first) is None  # 第 1 次没有可挪的
    second = record("todo", extra={"attempt": 2})
    target = attempts.archive(layout, second)
    assert target == layout.attempt_dir("0007", 1) == directory / "attempt_1"
    assert sorted(path.name for path in directory.iterdir()) == sorted([*kept, "attempt_1"])
    assert sorted(path.name for path in target.iterdir()) == sorted([*moved, "checkpoint", "36-implement.check.r1-raw"])
    assert attempts.archive(layout, second) is None  # 已挪过
    (directory / "31-implement.prepare-handoff.json").write_text("{}", encoding="utf-8")
    assert attempts.archive(layout, second) == target  # 中途中断后重做：同名的覆盖
