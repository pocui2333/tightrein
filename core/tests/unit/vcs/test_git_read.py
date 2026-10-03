import pytest

from tightrein.vcs.errors import GitCommandError, RefNotFound
from tightrein.vcs.git_read import GitReader, HeadState
from tightrein.vcs.process import VcsProcess


@pytest.fixture
def setup(repos):
    origin, repo = repos.origin_and_clone()
    return repos, origin, repo, GitReader(VcsProcess(environ=repos.environ))


def test_status_and_head_of_a_clean_clone(setup):
    repos, _, repo, git = setup
    status = git.status(repo)
    assert (status.branch, status.upstream, status.ahead, status.behind, status.clean) == (
        "main", "origin/main", 0, 0, True,
    )
    assert git.head(repo) == HeadState(repos.head(repo), "main")
    repos.write(repo, "bin/Debug/app.dll", "binary")
    assert git.status(repo).clean


def test_status_lists_changes(setup):
    repos, _, repo, git = setup
    repos.write(repo, "src/OrderService.cs", "changed\n")
    repos.write(repo, "notes/草稿 1.md", "draft\n")
    status = git.status(repo)
    assert not status.clean
    assert status.changed_paths == ("notes/草稿 1.md", "src/OrderService.cs")


def test_detached_head_and_worktree_list(setup):
    repos, _, repo, git = setup
    readonly = repo.parent / "worktrees" / "readonly"
    repos.git(repo, "worktree", "add", "-q", "--detach", str(readonly), "origin/main")
    assert git.head(readonly) == HeadState(repos.head(repo), None)
    listed = git.worktree_list(repo)
    assert [(item.path.endswith("readonly"), item.branch, item.detached) for item in listed] == [
        (False, "main", False), (True, None, True),
    ]


def test_refs_remotes_stash_and_git_dir(setup):
    repos, origin, repo, git = setup
    repos.git(repo, "tag", "v1")
    head = repos.head(repo)
    assert git.refs(repo) == {"refs/heads/main": head, "refs/tags/v1": head}
    remotes = git.remotes(repo)
    assert remotes["remote.origin.url"] == (str(origin),)
    assert git.local_config(repo, r"^remote\..*\.url$") == {"remote.origin.url": (str(origin),)}
    assert git.local_config(repo, r"^credential\.") == {}
    assert git.stash(repo) == ()
    repos.write(repo, "README.md", "stashed\n")
    repos.git(repo, "stash", "-q")
    assert len(git.stash(repo)) == 1
    assert git.git_dir(repo) == (repo / ".git").resolve()
    assert git.operations_in_progress(repo) == ()


def test_rev_parse_and_ancestry(setup):
    repos, _, repo, git = setup
    first = repos.head(repo)
    second = repos.commit(repo, "feat: second", {"src/New.cs": "class New {}\n"})
    assert git.rev_parse(repo, "HEAD~1") == first
    with pytest.raises(RefNotFound):
        git.rev_parse(repo, "no-such-branch")
    assert git.is_ancestor(repo, first, second)
    assert not git.is_ancestor(repo, second, first)
    with pytest.raises(RefNotFound):
        git.is_ancestor(repo, "0" * 40, second)
    assert git.branch_exists(repo, "main")
    assert not git.branch_exists(repo, "cty/fix-order")


def test_log_oneline_and_blame(setup):
    repos, _, repo, git = setup
    base = repos.head(repo)
    changed = "class OrderService\n{\n    int Page = 2;\n}\n"
    second = repos.commit(repo, "fix: 订单查询 500", {"src/OrderService.cs": changed})
    commits = git.log(repo, f"{base}..HEAD")
    assert [(commit.commit, commit.author, commit.subject) for commit in commits] == [
        (second, "Cui Ty", "fix: 订单查询 500"),
    ]
    assert len(git.log(repo, "HEAD", paths=["README.md"])) == 1
    assert len(git.log(repo, "HEAD", limit=1)) == 1
    assert git.oneline(repo, f"{base}..HEAD") == f"{second[:7]} fix: 订单查询 500\n"
    blame = git.blame(repo, "src/OrderService.cs", 2, 3)
    assert [(line.line, line.commit, line.content) for line in blame] == [
        (2, base, "{"), (3, second, "    int Page = 2;"),
    ]
    with pytest.raises(GitCommandError):
        git.blame(repo, "src/Missing.cs", 1, 1)


def test_diff_of_the_working_tree(setup):
    repos, _, repo, git = setup
    repos.write(repo, "src/OrderService.cs", "class OrderService\n{\n    [Authorize]\n    int Page = 1;\n}\n")
    (repo / "README.md").unlink()
    repos.write(repo, "src/Added.cs", "class Added {}\n")
    repos.git(repo, "add", "src/Added.cs")
    repos.write(repo, "notes.md", "untracked\n")
    diff = git.diff(repo, "HEAD")
    assert diff.paths == ("README.md", "src/Added.cs", "src/OrderService.cs")
    order = diff.files[2]
    assert (order.added, order.removed, order.added_lines) == (1, 0, ("    [Authorize]",))
    assert diff.files[0].removed_lines == ("# demo",)
    assert (diff.lines_added, diff.lines_removed) == (2, 1)
    assert git.diff(repo, "HEAD", paths=["src/Added.cs"]).paths == ("src/Added.cs",)
    repos.write(repo, "bin/app.dll", "ignored\n")
    assert git.untracked(repo) == ("notes.md",)
    assert git.untracked(repo, include_ignored=True) == ("bin/app.dll", "notes.md")
    assert "notes.md" in git.files(repo)
    assert "README.md" in git.files(repo)


def test_diff_between_commits_and_diff_hash(setup):
    repos, _, repo, git = setup
    base = repos.head(repo)
    clean = git.diff_hash(repo, base)
    repos.write(repo, "notes.md", "one\n")
    first = git.diff_hash(repo, base)
    repos.write(repo, "notes.md", "two\n")
    second = git.diff_hash(repo, base)
    assert len({clean, first, second}) == 3
    repos.write(repo, "notes.md", "one\n")
    assert git.diff_hash(repo, base) == first
    head = repos.commit(repo, "docs: notes")
    assert git.diff(repo, base, head).paths == ("notes.md",)


def test_fetch_branches_containing_and_conflicts(setup):
    repos, origin, repo, git = setup
    other = repos.root / "other"
    repos.git(repos.root, "clone", "-q", str(origin), str(other))
    upstream = repos.commit(other, "feat: upstream", {"src/OrderService.cs": "upstream\n"})
    repos.git(other, "push", "-q", "origin", "main")
    git.fetch(repo)
    assert git.rev_parse(repo, "origin/main") == upstream
    assert git.branches_containing(repo, upstream) == ["origin/main"]
    repos.commit(repo, "feat: local", {"src/OrderService.cs": "local\n"})
    result = git.process.git(repo, "merge", "--no-ff", "--no-edit", "origin/main", ok_codes=(0, 1))
    assert result.returncode == 1
    assert git.status(repo).unmerged == ("src/OrderService.cs",)
    assert git.conflict_files(repo) == ("src/OrderService.cs",)
    assert git.merge_head(repo) == upstream
    assert git.operations_in_progress(repo) == ("MERGE_HEAD",)
