"""Git：固定环境与低速断开、错误按退出码分类与只读重试、输出解析、diff_hash 与比较基准、写操作的幂等与对账。"""

from dataclasses import replace

import pytest

from tightrein.protocol.git.git import (
    CommandFailed,
    Git,
    MainBranchRefused,
    MergeConflict,
    NetworkError,
    ProgramNotFound,
    PushRejected,
    Stale,
    WorktreeDirty,
    _parse_numstat,
    changed,
    idempotency_key,
)
from tightrein.protocol.process import Outcome
from tightrein.protocol.raw import RawDir
from tightrein.store.tables import operations

ENV = {"PATH": "/usr/bin", "LANG": "zh_CN.UTF-8"}
FATAL = (128, "", "fatal: unable to access 'https://github.com/o/r/': Could not resolve host\n")


def scripted_git(tmp_path, settings, scripted, *outcomes):
    sleeps = []
    runner = scripted(*outcomes)
    return Git(tmp_path, runner, ENV, settings, sleep=sleeps.append), runner, sleeps


def stopped(reason):
    return Outcome(None, "", "", 1, reason, None)


# 调用方式与错误分类


def test_git_runs_with_a_fixed_environment_and_the_git_timeout(tmp_path, settings, scripted):
    git, runner, _ = scripted_git(tmp_path, settings, scripted, (0, "abc\n", ""))
    assert git.rev_parse("HEAD") == "abc"
    command = runner.commands[0]
    assert command.argv == ("git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    assert command.cwd == tmp_path and command.stdin is None
    assert command.env == {**ENV, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    assert command.timeout_s == 600


def test_remote_commands_disconnect_when_the_transfer_is_too_slow(tmp_path, settings, scripted):
    git, runner, _ = scripted_git(tmp_path, settings, scripted, (0, "", ""), (0, "", ""))
    git.fetch(prune=True)
    git.untracked()
    fetch, local = (command.argv for command in runner.commands)
    assert fetch == ("git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60", "fetch", "origin",
                     "--prune")
    assert "http.lowSpeedLimit=1000" not in local


def test_low_speed_limits_come_from_the_settings(tmp_path, settings_with, scripted):
    settings = settings_with({"limits": {"timeouts": {"gitLowSpeedBytes": 500, "gitLowSpeedTime": "2m"}}})
    git, runner, _ = scripted_git(tmp_path, settings, scripted, (0, "", ""))
    git.fetch()
    assert runner.commands[0].argv[1:5] == ("-c", "http.lowSpeedLimit=500", "-c", "http.lowSpeedTime=120")


def test_remote_failures_are_network_errors_and_reads_retry(tmp_path, settings, scripted):
    git, runner, sleeps = scripted_git(tmp_path, settings, scripted, FATAL, FATAL, (0, "", ""))
    git.fetch()
    # 全抖动：第 n 次重试在 0 到 min(上限, 起始 1s × 2^(n-1)) 之间随机
    assert len(runner.commands) == 3 and len(sleeps) == 2 and 0 <= sleeps[0] <= 1.0 and 0 <= sleeps[1] <= 2.0
    git, runner, sleeps = scripted_git(tmp_path, settings, scripted, FATAL, FATAL, FATAL)
    with pytest.raises(NetworkError):
        git.fetch()
    assert len(runner.commands) == 3 and len(sleeps) == 2 and 0 <= sleeps[0] <= 1.0 and 0 <= sleeps[1] <= 2.0


def test_timeouts_of_remote_commands_are_network_errors(tmp_path, settings, scripted):
    git, _, _ = scripted_git(tmp_path, settings, scripted, stopped("idle"), stopped("idle"), stopped("idle"),
                             stopped("timeout"))
    with pytest.raises(NetworkError, match="被终止"):
        git.fetch()
    with pytest.raises(CommandFailed) as caught:
        git.log("HEAD")
    assert not isinstance(caught.value, NetworkError)


def test_other_exit_codes_are_command_errors(tmp_path, settings, scripted):
    git, _, sleeps = scripted_git(tmp_path, settings, scripted, (128, "", "fatal: not a git repository\n"))
    with pytest.raises(CommandFailed) as caught:
        git.untracked()
    assert caught.value.stderr == "fatal: not a git repository"
    assert str(caught.value) == ("git ls-files --others --exclude-standard -z 退出码 128："
                                 "fatal: not a git repository")
    assert not isinstance(caught.value, NetworkError) and sleeps == []


def test_missing_programs_and_directories(tmp_path, settings, scripted):
    git, _, _ = scripted_git(tmp_path, settings, scripted, Outcome(None, "", "", 0, None, "No such file: git"))
    with pytest.raises(ProgramNotFound):
        git.untracked()
    gone, _, _ = scripted_git(tmp_path / "gone", settings, scripted)
    with pytest.raises(CommandFailed, match="工作目录不存在"):
        gone.untracked()


def test_error_text_is_redacted(tmp_path, settings, scripted):
    token = "ghp_abcdefghijklmnopqrstuvwx"

    class Masking:
        def text(self, value):
            return value.replace(token, "[REDACTED:secret]")

    runner = scripted((128, "", f"fatal: could not read from https://cty:{token}@github.com/o/r\n"))
    git = Git(tmp_path, runner, ENV, settings, redactor=Masking(), sleep=lambda seconds: None)
    with pytest.raises(CommandFailed) as caught:
        git.untracked()
    assert token not in str(caught.value) and "[REDACTED:secret]" in caught.value.stderr


def test_writes_are_not_retried(fix_branch, scope):
    repos, origin, git = fix_branch
    repos.git(git.repo, "remote", "set-url", "origin", str(origin.parent / "unreachable.git"))
    repos.commit(git.repo, "fix: one", {"a.txt": "a\n"})
    with pytest.raises(NetworkError):
        git.push(scope=scope)
    pushes = [command for command in git.runner.commands if "push" in command.argv]
    assert len(pushes) == 1


# 只读查询


def test_status_ignores_build_products_and_lists_changes(clone):
    repos, _, repo, git = clone
    status = git.status()
    assert (status.branch, status.clean) == ("main", True)
    assert git.head().branch == "main" and git.head().commit == repos.head(repo)
    repos.write(repo, "bin/Debug/app.dll", "binary")
    assert git.status().clean
    repos.write(repo, "src/OrderService.cs", "changed\n")
    repos.write(repo, "notes/草稿 1.md", "draft\n")
    assert git.status().changed_paths == ("notes/草稿 1.md", "src/OrderService.cs")


def test_numstat_counts_untracked_files_and_renames(clone):
    repos, _, repo, git = clone
    base = repos.head(repo)
    repos.write(repo, "src/OrderService.cs", "class OrderService\n{\n    [Authorize]\n    int Page = 1;\n}\n")
    repos.write(repo, "src/New.cs", "line 1\nline 2\n")
    repos.git(repo, "mv", "README.md", "README.zh.md")
    repos.write(repo, "bin/app.dll", "ignored")
    changes = git.numstat(base)
    assert [(change.path, change.added, change.deleted, change.status) for change in changes] == [
        ("README.zh.md", 0, 0, "R"), ("src/New.cs", 2, 0, "?"), ("src/OrderService.cs", 1, 0, "M"),
    ]
    assert git.changed_files(base) == ("README.md", "README.zh.md", "src/New.cs", "src/OrderService.cs")


def test_binary_files_count_zero_lines(clone):
    """git 对二进制文件的 numstat 给 `-`：行数记 0，不当成解析错误。"""
    repos, _, repo, git = clone
    (repo / "assets").mkdir()
    (repo / "assets" / "logo.png").write_bytes(b"\x89PNG\0\0\x01\x02")
    base = repos.commit(repo, "feat: logo")
    (repo / "assets" / "logo.png").write_bytes(b"\x89PNG\0\0\x03\x04\x05")
    assert [(change.path, change.added, change.deleted, change.status) for change in git.numstat(base)] == [
        ("assets/logo.png", 0, 0, "M"),
    ]
    assert _parse_numstat("-\t-\tassets/logo.png\0" "3\t1\tsrc/a.cs\0") == [("assets/logo.png", 0, 0),
                                                                          ("src/a.cs", 3, 1)]


def test_ancestry_is_three_valued(clone):
    repos, _, repo, git = clone
    first = repos.head(repo)
    second = repos.commit(repo, "feat: second", {"src/New.cs": "class New {}\n"})
    assert git.is_ancestor(first, second) is True
    assert git.is_ancestor(second, first) is False
    assert git.is_ancestor("0" * 40, second) is None
    assert git.is_newer(second, first) is True
    assert git.is_newer(first, first) is False
    assert git.is_newer(second, "0" * 40) is None and git.is_newer(None, first) is None


def test_sibling_commits_are_not_newer(clone):
    repos, _, repo, git = clone
    base = repos.head(repo)
    left = repos.commit(repo, "feat: left", {"a.txt": "a\n"})
    repos.git(repo, "checkout", "-q", "-b", "side", base)
    right = repos.commit(repo, "feat: right", {"b.txt": "b\n"})
    assert git.is_newer(right, left) is False


def test_log_blame_and_show(clone):
    repos, _, repo, git = clone
    base = repos.head(repo)
    second = repos.commit(repo, "fix: 订单查询 500", {"src/OrderService.cs": "class OrderService\n{\n    int Page = 2;\n}\n"})
    assert [(commit.commit, commit.author, commit.subject) for commit in git.log(f"{base}..HEAD")] == [
        (second, "Cui Ty", "fix: 订单查询 500")]
    assert [(line.line, line.commit) for line in git.blame("src/OrderService.cs", 2, 3)] == [(2, base), (3, second)]
    assert git.show(base, "README.md") == "# demo\n"
    assert git.show(base, "src/Missing.cs") is None


def test_diff_hash_does_not_depend_on_whether_the_change_is_committed(clone):
    repos, _, repo, git = clone
    base = repos.head(repo)
    clean = git.diff_hash(base)
    repos.write(repo, "notes.md", "one\n")
    first = git.diff_hash(base)
    repos.write(repo, "notes.md", "two\n")
    assert len({clean, first, git.diff_hash(base)}) == 3
    repos.write(repo, "notes.md", "one\n")
    assert git.diff_hash(base) == first
    repos.commit(repo, "docs: notes")
    assert git.diff_hash(base) == first


def test_diff_hash_against_the_merged_main_matches_the_reviewed_fix(fix_branch, scope):
    """合并只改了其他文件的 main 后，以合并进来的 main(review_base)为基准 diff_hash 不变；main 改到同一文件时不同。"""
    repos, origin, git = fix_branch
    base = git.review_base("origin/main")
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    repos.write(git.repo, "tests/test_order.py", "def test_order(): ...\n")
    reviewed = git.diff_hash(base)
    repos.commit(git.repo, "fix: order")
    assert git.diff_hash(base) == reviewed
    upstream = repos.upstream(origin, "feat: readme", {"README.md": "upstream\n"})
    git.fetch()
    assert git.review_base("origin/main") == base
    git.merge("origin/main", scope=scope)
    assert git.review_base("origin/main") == upstream
    assert git.diff_hash(base) != reviewed
    assert git.diff_hash(git.review_base("origin/main")) == reviewed
    overlapping = repos.upstream(origin, "feat: order", {"src/OrderService.cs": "fixed\nupstream\n"})
    git.fetch()
    repos.git(git.repo, "merge", "-q", "-X", "theirs", "--no-edit", "origin/main")
    assert git.diff_hash(overlapping) != reviewed


# 写操作


def test_commit_passes_the_message_through_stdin_and_runs_once(fix_branch, scope):
    repos, _, git = fix_branch
    base = repos.head(git.repo)
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    message = "fix: 订单查询 500\n\n分页上限写错。\n"
    commit = git.commit(["src/OrderService.cs"], message, base=base, scope=scope)
    assert git.head().commit == commit
    committed = [command for command in git.runner.commands if "commit" in command.argv]
    assert committed[0].argv[-2:] == ("-F", "-") and committed[0].stdin == message
    assert repos.git(git.repo, "log", "-1", "--format=%B").strip() == message.strip()
    assert git.commit(["src/OrderService.cs"], message, base=base, scope=scope) == commit
    assert len(git.log(f"{base}..HEAD")) == 1


def test_commit_messages_never_carry_ai_attribution(fix_branch, scope):
    repos, _, git = fix_branch
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    signed = "fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
    with pytest.raises(ValueError, match="AI 署名"):
        git.commit(["src/OrderService.cs"], signed, base="HEAD", scope=scope)
    assert git.log("origin/main..HEAD") == []


def test_an_interrupted_commit_is_reconciled(fix_branch, scope, interrupt):
    repos, _, git = fix_branch
    base = repos.head(git.repo)
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    key = idempotency_key(scope, "commit", (git.diff_hash(base), "src/OrderService.cs"))
    interrupt(key)
    done = repos.commit(git.repo, "fix: crash")
    assert git.commit(["src/OrderService.cs"], "fix: crash\n", base=base, scope=scope) == done
    assert operations.get(scope.conn, key).status == operations.DONE
    assert len(git.log(f"{base}..HEAD")) == 1


def test_an_interrupted_commit_that_did_nothing_runs_again(fix_branch, scope, interrupt):
    repos, _, git = fix_branch
    base = repos.head(git.repo)
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    interrupt(idempotency_key(scope, "commit", (git.diff_hash(base), "src/OrderService.cs")))
    git.commit(["src/OrderService.cs"], "fix: retry\n", base=base, scope=scope)
    assert [commit.subject for commit in git.log(f"{base}..HEAD")] == ["fix: retry"]


def test_writes_refuse_the_main_branch_and_a_detached_head(clone, scope):
    repos, _, repo, git = clone
    repos.write(repo, "src/OrderService.cs", "fixed\n")
    with pytest.raises(MainBranchRefused):
        git.commit(["src/OrderService.cs"], "fix: x\n", base="HEAD", scope=scope)
    with pytest.raises(MainBranchRefused):
        git.push(scope=scope)
    repos.git(repo, "checkout", "-q", "--detach")
    with pytest.raises(MainBranchRefused):
        git.merge("origin/main", scope=scope)


def test_merge_conflicts_are_reported_and_left_in_progress(fix_branch, scope):
    repos, origin, git = fix_branch
    repos.commit(git.repo, "fix: local", {"README.md": "local\n"})
    upstream = repos.upstream(origin, "feat: upstream", {"README.md": "upstream\n"})
    git.fetch()
    with pytest.raises(MergeConflict) as caught:
        git.merge("origin/main", scope=scope)
    assert caught.value.files == ("README.md",)
    assert git.merge_head() == upstream


def test_push_and_rejected_pushes(fix_branch, scope):
    repos, origin, git = fix_branch
    first = repos.commit(git.repo, "fix: one", {"a.txt": "a\n"})
    assert git.push(scope=scope) == first
    assert git.rev_parse("origin/cty/fix-order") == first
    other = repos.root / "other-fix"
    repos.git(repos.root, "clone", "-q", "-b", "cty/fix-order", str(origin), str(other))
    repos.commit(other, "fix: elsewhere", {"b.txt": "b\n"})
    repos.git(other, "push", "-q", "origin", "cty/fix-order")
    repos.commit(git.repo, "fix: two", {"c.txt": "c\n"})
    with pytest.raises(PushRejected) as caught:
        git.push(scope=scope)
    assert caught.value.rejected == ("refs/heads/cty/fix-order:refs/heads/cty/fix-order",)


def test_an_interrupted_push_is_reconciled_against_the_remote(fix_branch, scope, interrupt):
    repos, _, git = fix_branch
    commit = repos.commit(git.repo, "fix: one", {"a.txt": "a\n"})
    interrupt(idempotency_key(scope, "push", ("cty/fix-order", commit)))
    repos.git(git.repo, "push", "-q", "-u", "origin", "cty/fix-order")
    pushes_before = sum("push" in command.argv for command in git.runner.commands)
    assert git.push(scope=scope) == commit
    assert sum("push" in command.argv for command in git.runner.commands) == pushes_before


def test_an_interrupted_merge_of_main_is_reconciled(fix_branch, scope, interrupt):
    """上次合并主干被中断：主干已并入(没有 MERGE_HEAD、目标是 HEAD 的祖先)就补记完成，不再合并一次。"""
    repos, origin, git = fix_branch
    repos.commit(git.repo, "fix: local", {"a.txt": "a\n"})
    repos.upstream(origin, "feat: upstream", {"b.txt": "b\n"})
    git.fetch()
    target = git.rev_parse("origin/main")
    key = idempotency_key(scope, "merge", ("cty/fix-order", target))
    interrupt(key)
    repos.git(git.repo, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
    merged = repos.head(git.repo)
    merges_before = sum("merge" in command.argv for command in git.runner.commands)
    assert git.merge("origin/main", scope=scope) == merged
    assert sum("merge" in command.argv for command in git.runner.commands) == merges_before
    assert operations.get(scope.conn, key).status == operations.DONE


def test_an_interrupted_merge_that_did_nothing_runs_again(fix_branch, scope, interrupt):
    repos, origin, git = fix_branch
    repos.commit(git.repo, "fix: local", {"a.txt": "a\n"})
    upstream = repos.upstream(origin, "feat: upstream", {"b.txt": "b\n"})
    git.fetch()
    interrupt(idempotency_key(scope, "merge", ("cty/fix-order", upstream)))
    head = git.merge("origin/main", scope=scope)
    assert git.is_ancestor(upstream, head) and git.merge_head() is None


# 每条命令的输出按步存到 raw


def test_each_command_of_a_write_is_saved_as_a_step_log(fix_branch, scope, tmp_path):
    """范围带原始输出目录时，写操作的每条命令(已脱敏)按顺序存为 vcs/<对象>/<操作>/step-N.log；失败的那一步也存下。"""
    repos, origin, git = fix_branch
    raw = RawDir(tmp_path / "raw")
    logged = replace(scope, raw=raw)
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    git.commit(["src/OrderService.cs"], "fix: order\n", base="HEAD", scope=logged)
    commit_steps = [path for path in raw.files() if path.startswith("vcs/0007/commit/")]
    assert commit_steps[:2] == ["vcs/0007/commit/step-1.log", "vcs/0007/commit/step-2.log"]
    first = raw.path(commit_steps[0]).read_text(encoding="utf-8")
    assert first.startswith("$ git add -- src/OrderService.cs\nexit: 0\n")
    assert raw.path(commit_steps[1]).read_text(encoding="utf-8").startswith("$ git commit -F -\n")
    repos.git(git.repo, "remote", "set-url", "origin", str(origin.parent / "unreachable.git"))
    with pytest.raises(NetworkError):
        git.push(scope=logged)
    push_steps = [path for path in raw.files() if path.startswith("vcs/0007/push/")]
    last = raw.path(push_steps[-1]).read_text(encoding="utf-8")
    assert "push --porcelain -u origin cty/fix-order" in last and "exit: 128" in last


def test_step_logs_continue_numbering_and_are_skipped_without_a_raw_dir(fix_branch, scope, tmp_path):
    repos, _, git = fix_branch
    raw = RawDir(tmp_path / "raw")
    raw.write_text("vcs/0007/commit/step-1.log", "上一次\n")
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    git.commit(["src/OrderService.cs"], "fix: order\n", base="HEAD", scope=replace(scope, raw=raw))
    assert raw.path("vcs/0007/commit/step-1.log").read_text(encoding="utf-8") == "上一次\n"
    assert raw.path("vcs/0007/commit/step-2.log").read_text(encoding="utf-8").startswith("$ git add")
    repos.write(git.repo, "a.txt", "a\n")
    git.commit(["a.txt"], "fix: a\n", base="HEAD", scope=scope)
    assert not any("a.txt" in raw.path(path).read_text(encoding="utf-8") for path in raw.files())


# 前置条件


def test_a_write_is_stale_when_the_state_observed_at_decision_time_changed(fix_branch, scope):
    """做决定时记下 HEAD 与改动哈希，执行前又有人提交：判过期、不执行，键删去，状态回到判断时的样子后可以再做。"""
    repos, _, git = fix_branch
    base = repos.head(git.repo)
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    decided = git.state(("branch", "head", "diffHash"), base=base)
    repos.commit(git.repo, "chore: someone else", {"docs/a.md": "a\n"})
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    with pytest.raises(Stale) as caught:
        git.commit(["src/OrderService.cs"], "fix: x\n", base=base, scope=scope, expected=decided)
    assert caught.value.differences == ("diffHash", "head")
    assert [commit.subject for commit in git.log(f"{base}..HEAD")] == ["chore: someone else"]
    assert operations.find(scope.conn) == []
    with pytest.raises(Stale, match="originMain"):
        git.merge("origin/main", scope=scope, expected={"originMain": "0" * 40})


def test_preconditions_are_checked_only_after_the_idempotency_key(fix_branch, scope):
    """已完成的操作直接返回上次结果，不复核：做完之后 HEAD 已变，先复核会被误判为过期。"""
    repos, _, git = fix_branch
    base = repos.head(git.repo)
    repos.write(git.repo, "src/OrderService.cs", "fixed\n")
    decided = git.state(base=base)
    commit = git.commit(["src/OrderService.cs"], "fix: x\n", base=base, scope=scope, expected=decided)
    assert git.state(("head",))["head"] == commit != decided["head"]
    assert git.commit(["src/OrderService.cs"], "fix: x\n", base=base, scope=scope, expected=decided) == commit
    pushed = git.state(("branch", "head"))
    assert git.push(scope=scope, expected=pushed) == commit
    assert git.push(scope=scope, expected=pushed) == commit


def test_state_reads_only_the_requested_items(clone):
    repos, _, repo, git = clone
    head = repos.head(repo)
    assert git.state(("branch", "head", "originMain")) == {"branch": "main", "head": head, "originMain": head}
    assert git.state(("head",)) == {"head": head}
    with pytest.raises(ValueError, match="不能观察"):
        git.state(("tags",))
    assert changed({"head": "a", "branch": "b"}, {"head": "a"}) == ["branch"]


def test_a_dirty_worktree_is_not_removed(clone, scope):
    repos, _, repo, git = clone
    worktree = repo.parent / "worktrees" / "0007"
    git.worktree_add(worktree, branch="cty/fix-order", base="origin/main", scope=scope)
    repos.write(worktree, "src/OrderService.cs", "未提交\n")
    with pytest.raises(WorktreeDirty) as caught:
        git.worktree_remove(worktree, scope=scope)
    assert caught.value.paths == ("src/OrderService.cs",) and worktree.exists()


def test_paths_with_spaces_and_chinese_need_no_quoting(clone):
    repos, _, repo, git = clone
    base = repos.head(repo)
    repos.write(repo, "docs/说明 文档.md", "x\n")
    assert git.changed_files(base) == ("docs/说明 文档.md",)
    assert git.ls_files() == tuple(sorted((".gitignore", "README.md", "docs/说明 文档.md", "src/OrderService.cs",
                                           "tests/OrderTests.cs")))
