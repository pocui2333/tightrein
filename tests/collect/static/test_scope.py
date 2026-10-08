from tightrein.collect.static import scope
from tightrein.collect.static.scope import Level


def test_incremental_scope_is_the_existing_changed_files(static_runtime, repos):
    git = static_runtime().git
    base = repos.git(repos.repo, "rev-parse", "HEAD").strip()
    repos.push("feat: 改动", {"src/orders.py": "def a():\n    pass\n", "src/new.py": "x = 1\n"})
    repos.git(repos.repo, "rm", "-q", "tests/test_orders.py")
    head = repos.push("chore: 删除测试", {})
    found = scope.resolve(git, base, Level.INCREMENTAL)
    assert (found.level, found.base, found.head) == (Level.INCREMENTAL, base, head)
    assert found.files == ("src/new.py", "src/orders.py") == found.changed and not found.whole_repo


def test_no_new_commits_skips_only_the_incremental_level(static_runtime, repos):
    git = static_runtime().git
    head = repos.git(repos.repo, "rev-parse", "HEAD").strip()
    assert scope.resolve(git, head[:12], Level.INCREMENTAL) is None
    full = scope.resolve(git, head, Level.FULL)
    assert full.files == ("README.md", "src/orders.py", "tests/test_orders.py") and full.changed == ()


def test_the_first_run_is_a_baseline_over_everything(static_runtime):
    found = scope.resolve(static_runtime().git, None, Level.INCREMENTAL)
    assert found.level is Level.BASELINE and found.first_run and found.files == found.changed
    assert len(found.files) == 3


def test_non_code_changes_and_reviewed_content_are_not_reviewed(tmp_path):
    kept, skipped = scope.reviewable(["README.md", "src/a.py", "tests/test_a.py", "poetry.lock"],
                                     ["*.md", "*.lock", "tests/"])
    assert (kept, skipped) == (["src/a.py"], 3)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    hashes = scope.content_hashes(tmp_path, ["a.py", "b.py"])
    assert scope.unreviewed(hashes, {"a.py": hashes["a.py"], "b.py": "old"}) == ["b.py"]
