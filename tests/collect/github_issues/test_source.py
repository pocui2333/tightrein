"""GitHub 上已有的 Issue：报告与需求两类信号、跳过已关联与处理过的编号、记下处理过的；没启用同步时不跑。"""

from datetime import UTC, datetime

import pytest

from tightrein.collect import collect as collecting
from tightrein.collect.common.source import SourceStatus, SourceUnavailable
from tightrein.collect.github_issues import source
from tightrein.protocol.git import GitError
from tightrein.protocol.git.github import OpenIssue
from tightrein.store.tables import issues, state
from tightrein.store.tables.issues import Issue

CREATED = datetime(2026, 10, 4, 8, 30, tzinfo=UTC)


class FakeHub:
    slug = "acme/shop"

    def __init__(self, found):
        self.found = list(found)
        self.error = None

    def open_issues(self):
        if self.error is not None:
            raise self.error
        return list(self.found)


def opened(number, title, body="", labels=()):
    return OpenIssue(number, title, body, tuple(labels), f"https://github.com/acme/shop/issues/{number}", CREATED)


def runtime(source_runtime, found, *, enabled=True):
    current = source_runtime(modules={"release.github_issues": {"status": "enabled" if enabled else "disabled"}})
    current.github = FakeHub(found)
    return current


def save(current, result):
    for key, value in result.state.items():
        state.put(current.conn, key, value, current.clock)


def test_open_issues_become_report_and_request_signals(source_runtime):
    current = runtime(source_runtime, [
        opened(12, "导出订单时报 500", "点导出就报错，堆栈指向 `services/export.py:42`，token sk-live-123456", ["bug"]),
        opened(15, "加个暗色模式", "希望支持暗色", ["Enhancement"]),
    ])
    current.redactor.register("sk-live-123456")
    result = source.collect(current)
    assert result.status is SourceStatus.DONE and result.coverage == [] and result.read == 2
    report, request = result.signals
    assert (report.check_type, report.location, report.message, report.deterministic) == (
        "report", "services/export.py:42", "导出订单时报 500", True)
    assert (request.check_type, request.location, request.deterministic) == ("request", None, True)
    assert report.group_key == "github-issue:12" and report.occurred_at == "2026-10-04T08:30:00Z"
    assert report.evidence["number"] == 12 and report.evidence["url"].endswith("/issues/12")
    assert report.evidence["labels"] == ["bug"] and "sk-live-123456" not in report.evidence["body"]
    assert result.state == {source.SEEN_KEY: [12, 15]}


def test_linked_and_seen_numbers_are_skipped_and_seen_ones_are_saved(source_runtime):
    current = runtime(source_runtime, [opened(7, "自己建的镜像"), opened(12, "导出报错"), opened(15, "新的")])
    issues.save(current.conn, Issue(id="0003", status="todo", title="镜像", kind="bug", origin="problem",
                                    extra={"github": {"number": 7, "url": "u", "state": "open", "label": None}}),
                current.clock)
    state.put(current.conn, source.SEEN_KEY, [12], current.clock)
    result = source.collect(current)
    assert [signal.evidence["number"] for signal in result.signals] == [15]
    assert result.state[source.SEEN_KEY] == [12, 15]
    save(current, result)
    again = source.collect(current)
    assert (again.status, again.reason, again.signals) == (SourceStatus.SKIPPED, source.NOTHING_NEW, [])


def test_long_bodies_are_cut_and_bare_locations_are_found(source_runtime):
    text = "见 https://x.io/a/b.py:9 与 src/app/main.py:120 附近" + "长" * 5000
    current = runtime(source_runtime, [opened(3, "报错", text)])
    [signal] = source.collect(current).signals
    assert signal.location == "src/app/main.py:120"
    assert len(signal.evidence["body"]) == 4000


def test_paths_inside_urls_are_not_taken_as_the_location():
    text = "代理 http://127.0.0.1:8118/src/app/main.py:3 或 https://ci.example.com/log?file=src/app/main.py:5 报错"
    assert source.location_in(text) is None
    assert source.location_in(text + "，见 src/app/main.py:120") == "src/app/main.py:120"
    bare = "代理 127.0.0.1:8118/src/app/main.py:3、proxy.example.com:3128/src/app/main.py:4 报错"  # 不带协议头
    assert source.location_in(bare) is None
    assert source.location_in(bare + "，见 src/app/main.py:120") == "src/app/main.py:120"


def test_github_failures_fail_the_source_and_disabled_sync_skips_it(source_runtime):
    current = runtime(source_runtime, [])
    current.github.error = GitError("gh: 502")
    with pytest.raises(SourceUnavailable, match="gh: 502"):
        source.collect(current)
    off = runtime(source_runtime, [opened(1, "x")], enabled=False)
    assert source.collect(off).status is SourceStatus.SKIPPED


def test_the_source_follows_the_github_issues_switch(source_runtime):
    on = runtime(source_runtime, [])
    assert collecting.sources(on)[-1] == source.SOURCE
    assert source.SOURCE not in collecting.disabled_sources(on)
    assert source.SOURCE not in collecting.sources(runtime(source_runtime, [], enabled=False))
    on.github = None  # origin 不是 GitHub 仓库
    assert source.SOURCE not in collecting.sources(on)
