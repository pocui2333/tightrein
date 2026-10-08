"""同步 main：main 没动时不做事；只改别的文件时合并且不必重审；改到同一文件时报交集(从 merge-base 算)；冲突时
写报告、记下冲突文件并停下，不自行取舍；用户解决后检查冲突标记、记下取了哪一侧再完成合并。"""

import pytest

from tightrein.protocol.git import Stale
from tightrein.release.record import ReleaseBlocked, ReleaseState, parse_delivery
from tightrein.release.sync import BOTH, DECISION_KEYS, FIX_SIDE, MAIN_SIDE, hunks, markers_left, resolution, sync


def _fix(kit, repos, worktree, files):
    repos.commit(worktree, "fix: page", files)
    return parse_delivery(kit.delivery_facts(worktree, list(files)), "0007")


def test_nothing_happens_when_main_did_not_move(kit, runtime, repos, worktree):
    found = _fix(kit, repos, worktree, {"src/order.py": "PAGE = 0\n"})
    result = sync(runtime, "0007", found, ReleaseState(), runtime.git.at(worktree))
    assert result.merged is None and not result.needs_review


def test_main_changing_other_files_merges_without_review(kit, runtime, repos, worktree):
    found = _fix(kit, repos, worktree, {"src/order.py": "PAGE = 0\n"})
    main = repos.upstream("feat: user", {"src/user.py": "NAME = 'b'\n"})
    result = sync(runtime, "0007", found, ReleaseState(), runtime.git.at(worktree))
    assert result.main == main and result.merged == repos.head(worktree)
    assert result.overlap == () and not result.needs_review
    parents = repos.git(worktree, "log", "-1", "--format=%P").split()
    assert len(parents) == 2  # merge --no-ff，不 rebase


def test_overlap_counts_only_files_main_changed(kit, runtime, repos, worktree):
    found = _fix(kit, repos, worktree, {"src/order.py": kit.FIX_ORDER, "README.md": "# demo fixed\n"})
    repos.upstream("feat: order size", {"src/order.py": kit.MAIN_ORDER})
    result = sync(runtime, "0007", found, ReleaseState(), runtime.git.at(worktree))
    assert result.overlap == ("src/order.py",) and result.needs_review


def test_conflicts_are_reported_and_resolved_by_the_user(kit, runtime, repos, worktree):
    found = _fix(kit, repos, worktree, {"src/order.py": "PAGE = 0\n"})
    repos.upstream("fix: page two", {"src/order.py": "PAGE = 2\n"})
    state = ReleaseState()
    git = runtime.git.at(worktree)
    with pytest.raises(ReleaseBlocked, match="冲突：src/order.py") as blocked:
        sync(runtime, "0007", found, state, git)
    assert state.conflicts == ["src/order.py"]
    report = next(runtime.workspace.issue_dir("0007").glob("41-release.pr-evidence.md")).read_text(encoding="utf-8")
    assert "PAGE = 0" in report and "PAGE = 2" in report and "fix: page two" in report
    assert "git merge --abort" in " ".join(blocked.value.options)
    assert (worktree / "src/order.py").read_text(encoding="utf-8").startswith("<<<<<<< ")  # 冲突文件原样不动

    with pytest.raises(ReleaseBlocked, match="没解决的冲突"):
        sync(runtime, "0007", found, state, git)  # 用户还没处理
    (worktree / "src/order.py").write_text("PAGE = 2\n", encoding="utf-8")
    repos.git(worktree, "add", "src/order.py")
    result = sync(runtime, "0007", found, state, git)
    assert result.resolutions == {"src/order.py": MAIN_SIDE} and result.needs_review
    assert state.conflicts == [] and git.merge_head() is None


def test_conflict_helpers():
    text = "a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\nb\n"
    assert hunks(text) == [("ours", "theirs")]
    assert resolution("x", "y", "x") == FIX_SIDE and resolution("x", "y", "y") == MAIN_SIDE
    assert resolution("x", "y", "xy") == BOTH


def test_markers_left(tmp_path):
    (tmp_path / "a.py").write_text("<<<<<<< HEAD\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("ok\n", encoding="utf-8")
    assert markers_left(tmp_path, ["a.py", "b.py", "gone.py"]) == ["a.py"]


def test_the_merge_rechecks_the_state_seen_when_deciding(kit, runtime, repos, worktree, monkeypatch):
    found = _fix(kit, repos, worktree, {"src/order.py": "PAGE = 0\n"})
    repos.upstream("feat: user", {"src/user.py": "NAME = 'b'\n"})
    git = runtime.git.at(worktree)
    before = repos.head(worktree)
    seen = kit.stale_at_decision(monkeypatch, git)
    with pytest.raises(Stale, match="head"):
        sync(runtime, "0007", found, ReleaseState(), git)
    assert set(seen[0]) == set(DECISION_KEYS) and repos.head(worktree) == before  # 没有合并
    assert sync(runtime, "0007", found, ReleaseState(), git).merged == repos.head(worktree)
