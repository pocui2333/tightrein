from datetime import datetime, timezone

import pytest

from tightrein.vcs import parse
from tightrein.vcs.parse import (
    BlameLine,
    FilePatch,
    FileStat,
    StatusEntry,
    WorktreeInfo,
)

HEAD = "d6f37025" + "0" * 32
OTHER = "b136fc9e" + "1" * 32


def test_status_with_branch_headers_and_every_entry_kind():
    text = "\0".join([
        f"# branch.oid {HEAD}", "# branch.head cty/fix-order", "# branch.upstream origin/cty/fix-order",
        "# branch.ab +2 -1",
        "1 .M N... 100644 100644 100644 aaaa bbbb src/Order Service.cs",
        "2 R. N... 100644 100644 100644 aaaa bbbb R100 src/New.cs", "src/Old.cs",
        "u UU N... 100644 100644 100644 100644 aaaa bbbb cccc src/Conflict.cs",
        "? notes/草稿.md", "! bin/Debug/app.dll", "",
    ])
    status = parse.parse_status(text)
    assert (status.commit, status.branch, status.upstream, status.ahead, status.behind) == (
        HEAD, "cty/fix-order", "origin/cty/fix-order", 2, 1,
    )
    assert status.entries == (
        StatusEntry("src/Order Service.cs", "changed", ".", "M"),
        StatusEntry("src/New.cs", "renamed", "R", ".", "src/Old.cs"),
        StatusEntry("src/Conflict.cs", "unmerged", "U", "U"),
        StatusEntry("notes/草稿.md", "untracked"),
        StatusEntry("bin/Debug/app.dll", "ignored"),
    )
    assert not status.clean
    assert status.unmerged == ("src/Conflict.cs",)
    assert status.changed_paths == ("notes/草稿.md", "src/Conflict.cs", "src/New.cs", "src/Order Service.cs")


def test_status_of_a_detached_clean_worktree():
    status = parse.parse_status(f"# branch.oid {HEAD}\0# branch.head (detached)\0! obj/x.o\0")
    assert (status.branch, status.upstream, status.clean) == (None, None, True)
    assert parse.parse_status("# branch.oid (initial)\0# branch.head main\0").commit is None


def test_unknown_status_entry_is_rejected():
    with pytest.raises(ValueError):
        parse.parse_status("x something\0")


def test_refs_and_config():
    assert parse.parse_refs(f"refs/heads/main\0{HEAD}\nrefs/tags/v1\0{OTHER}\n") == {
        "refs/heads/main": HEAD, "refs/tags/v1": OTHER,
    }
    text = "remote.origin.url\nhttps://github.com/o/r.git\0remote.origin.fetch\n+refs/heads/*:refs/remotes/origin/*\0" \
           "remote.origin.fetch\n+refs/pull/*:refs/remotes/origin/pr/*\0"
    assert parse.parse_config(text) == {
        "remote.origin.url": ("https://github.com/o/r.git",),
        "remote.origin.fetch": ("+refs/heads/*:refs/remotes/origin/*", "+refs/pull/*:refs/remotes/origin/pr/*"),
    }


def test_worktree_list():
    text = "\0".join([
        "worktree /repo", f"HEAD {HEAD}", "branch refs/heads/main", "",
        "worktree /ws/worktrees/readonly", f"HEAD {OTHER}", "detached", "",
        "worktree /ws/worktrees/fix-0007", f"HEAD {HEAD}", "branch refs/heads/cty/fix-order", "locked", "",
    ]) + "\0"
    assert parse.parse_worktrees(text) == [
        WorktreeInfo("/repo", HEAD, "main"),
        WorktreeInfo("/ws/worktrees/readonly", OTHER, None, detached=True),
        WorktreeInfo("/ws/worktrees/fix-0007", HEAD, "cty/fix-order", locked=True),
    ]


def test_log_records():
    text = "\0".join([HEAD, "Cui Ty", "2026-09-29T11:15:03+09:00", "fix: 订单查询 500",
                      OTHER, "Cui Ty", "2026-09-28T10:00:00+09:00", "chore: init"]) + "\0"
    commits = parse.parse_log(text)
    assert [(commit.commit, commit.author, commit.subject) for commit in commits] == [
        (HEAD, "Cui Ty", "fix: 订单查询 500"), (OTHER, "Cui Ty", "chore: init"),
    ]
    assert commits[0].time == datetime(2026, 9, 29, 2, 15, 3, tzinfo=timezone.utc)
    assert parse.parse_log("") == []
    with pytest.raises(ValueError):
        parse.parse_log("a\0b\0")


def test_blame_reuses_commit_details():
    text = "\n".join([
        f"{HEAD} 10 12 2", "author Cui Ty", "author-mail <cty@example.com>", "author-time 1790000000",
        "author-tz +0900", "summary fix", "filename src/A.cs", "\tvar a = 1;",
        f"{HEAD} 11 13", "\tvar b = 2;",
    ]) + "\n"
    when = datetime.fromtimestamp(1790000000, timezone.utc)
    assert parse.parse_blame(text) == [
        BlameLine(12, HEAD, "Cui Ty", "cty@example.com", when, "var a = 1;"),
        BlameLine(13, HEAD, "Cui Ty", "cty@example.com", when, "var b = 2;"),
    ]


def test_numstat_with_rename_and_binary():
    text = "3\t1\tsrc/A.cs\0" + "0\t0\t\0src/Old.cs\0src/New.cs\0" + "-\t-\tdocs/logo.png\0"
    stats = parse.parse_numstat(text)
    assert stats == [
        FileStat("src/A.cs", 3, 1), FileStat("src/New.cs", 0, 0, "src/Old.cs"), FileStat("docs/logo.png", None, None),
    ]
    assert stats[2].binary


def test_patch_lines_with_line_numbers():
    text = "\n".join([
        "diff --git a/src/A.cs b/src/A.cs", "index 1..2 100644", "--- a/src/A.cs", "+++ b/src/A.cs",
        "@@ -3 +3,2 @@ class A", "-    [Authorize]", "+    [AllowAnonymous]", "+    // 允许匿名",
        "@@ -10,0 +12 @@", "+    return 42;",
        "diff --git a/src/Gone.cs b/src/Gone.cs", "deleted file mode 100644", "--- a/src/Gone.cs", "+++ /dev/null",
        "@@ -1 +0,0 @@", "-class Gone {}",
        "diff --git a/src/New.cs b/src/New.cs", "new file mode 100644", "--- /dev/null", "+++ b/src/New.cs",
        "@@ -0,0 +1 @@", "+class New {}", "",
    ])
    assert parse.parse_patch(text) == [
        FilePatch("src/A.cs", ("    [AllowAnonymous]", "    // 允许匿名", "    return 42;"), ("    [Authorize]",)),
        FilePatch("src/Gone.cs", (), ("class Gone {}",)),
        FilePatch("src/New.cs", ("class New {}",), ()),
    ]


def test_push_porcelain_flags():
    text = "To github.com:o/r.git\n!\trefs/heads/cty/fix:refs/heads/cty/fix\t[rejected] (fetch first)\nDone\n"
    assert parse.parse_push(text).rejected == ("refs/heads/cty/fix:refs/heads/cty/fix",)
    accepted = "To github.com:o/r.git\n*\trefs/heads/cty/fix:refs/heads/cty/fix\t[new branch]\nDone\n"
    assert parse.parse_push(accepted).rejected == ()


def test_gh_pulls():
    pull = parse.parse_pull({
        "number": 186, "url": "https://github.com/o/r/pull/186", "state": "MERGED", "mergeable": "UNKNOWN",
        "mergedAt": "2026-09-30T01:00:00Z", "mergeCommit": {"oid": HEAD}, "closedAt": "2026-09-30T01:00:00Z",
        "reviewDecision": "APPROVED", "headRefName": "cty/fix-order", "comments": [],
        "reviews": [{"state": "APPROVED"}],
    })
    assert (pull.number, pull.state, pull.merge_commit, pull.head_ref) == (186, "MERGED", HEAD, "cty/fix-order")
    assert pull.merged_at == datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
    brief = parse.parse_pulls('[{"number": 7, "url": "https://x/7", "state": "OPEN"}]')[0]
    assert (brief.merge_commit, brief.merged_at, brief.reviews) == (None, None, ())


def test_nul_separated_paths():
    assert list(parse.iter_nul("a b.cs\0中文.md\0")) == ["a b.cs", "中文.md"]
