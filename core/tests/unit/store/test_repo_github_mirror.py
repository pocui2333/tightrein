"""github_mirror 与 github_mirror_comments：镜像簿记的读写与待发评论。"""

from dataclasses import replace
from datetime import timedelta

from tightrein.store.repos import github_mirror
from tightrein.store.repos.github_mirror import MirrorState

from conftest import NOW


def test_missing_state_is_blank_and_failures_keep_other_fields(conn):
    assert github_mirror.get(conn, "0001") == MirrorState("0001")
    github_mirror.save(conn, MirrorState("0001", github_mirror.OPEN, "todo", synced_at=NOW))
    github_mirror.record_failure(conn, "0001", "gh 退出码 1", NOW + timedelta(minutes=1))
    state = github_mirror.get(conn, "0001")
    assert (state.remote_state, state.labelled_status, state.error, state.failed_at) == (
        "open", "todo", "gh 退出码 1", NOW + timedelta(minutes=1))
    github_mirror.save(conn, replace(state, error=None, failed_at=None))
    assert github_mirror.find(conn)[0].error is None


def test_comments_are_queued_in_order_and_marked_posted(conn):
    first = github_mirror.queue_comment(conn, "0001", "已放行", NOW)
    github_mirror.queue_comment(conn, "0002", "PR 已创建", NOW)
    github_mirror.queue_comment(conn, "0001", "已关闭", NOW)
    assert [item.body for item in github_mirror.unposted(conn, "0001")] == ["已放行", "已关闭"]
    github_mirror.mark_posted(conn, first, NOW)
    assert [item.body for item in github_mirror.unposted(conn)] == ["PR 已创建", "已关闭"]
