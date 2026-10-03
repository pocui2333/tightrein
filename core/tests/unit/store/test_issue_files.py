from dataclasses import replace

import pytest

from tightrein.contracts.validate import validate
from tightrein.domain.enums import IssueStatus
from tightrein.domain.issue import GithubLink
from tightrein.store import sequences
from tightrein.store.files import issue_files, markdown
from tightrein.store.files.issue_files import IssueDocument, IssueFileConflict, IssueFileError
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.migrations.runner import open_database
from tightrein.store.repos import issues

from store_samples import closed_issue, issue

BODY = "\n# 订单查询缺少归属校验\n\n## 历史\n\n- 2026-09-29 创建\n"
RUN = "R-20260929-021503-issue"


@pytest.fixture
def layout(tmp_path):
    return WorkspaceLayout(tmp_path / "sample")


def document(item=None, body=BODY):
    return IssueDocument(item or issue(), body, RUN)


def test_frontmatter_matches_the_schema_for_open_and_closed_issues():
    assert validate("handoff/frontmatter/issue.schema.json", issue_files.to_frontmatter(issue(), RUN)) == []
    closed = issue_files.to_frontmatter(closed_issue(), None)
    assert validate("handoff/frontmatter/issue.schema.json", closed) == []
    assert (closed["closeReason"], closed["hold"]) == ("duplicate", None)


def test_write_then_read_round_trip(conn, layout):
    record = issue_files.write(conn, layout, document())
    path = layout.issue_file("0007", "order-owner-check")
    assert record.path == "issues/0007-order-owner-check.md"
    assert record.file_sha256 == issue_files.content_hash(path.read_text(encoding="utf-8"))
    assert issues.get(conn, "0007") == record
    assert issue_files.read(path) == document()
    assert markdown.read(path).frontmatter["created"] == "2026-09-29T02:15:03Z"


def test_github_link_round_trips_and_old_files_have_none(conn, layout):
    linked = replace(issue(), github=GithubLink(12, "https://github.com/owner/name/issues/12"))
    record = issue_files.write(conn, layout, document(linked))
    assert issue_files.read(layout.root / record.path).issue.github == linked.github
    assert issues.get(conn, "0007").issue.github == linked.github
    data = issue_files.to_frontmatter(issue(), RUN)
    del data["github"]
    assert validate("handoff/frontmatter/issue.schema.json", data) == []
    assert issue_files.from_frontmatter(data, "order-owner-check").issue.github is None


def test_dependencies_round_trip(conn, layout):
    queued = replace(issue(), depends_on="0006")
    record = issue_files.write(conn, layout, document(queued))
    assert issue_files.read(layout.root / record.path).issue.depends_on == "0006"
    assert issues.get(conn, "0007").issue.depends_on == "0006"


def test_writing_again_after_our_own_write_is_allowed(conn, layout):
    issue_files.write(conn, layout, document())
    record = issue_files.write(conn, layout, document(issue(status=IssueStatus.TODO, hold=None)))
    assert issues.get(conn, "0007") == record
    assert record.issue.status is IssueStatus.TODO


def test_user_edits_are_not_overwritten(conn, layout):
    issue_files.write(conn, layout, document())
    path = layout.issue_file("0007", "order-owner-check")
    path.write_text(path.read_text(encoding="utf-8") + "\n用户补充的说明\n", encoding="utf-8")
    with pytest.raises(IssueFileConflict, match="reindex"):
        issue_files.write(conn, layout, document(issue(status=IssueStatus.TODO, hold=None)))
    assert issue_files.reindex(conn, layout).updated == ("0007",)
    issue_files.write(conn, layout, document(issue(status=IssueStatus.TODO, hold=None), BODY + "\n用户补充的说明\n"))
    assert "用户补充的说明" in path.read_text(encoding="utf-8")


def test_an_unindexed_file_is_not_overwritten(conn, layout):
    issue_files.write(conn, layout, document())
    issues.remove(conn, "0007")
    with pytest.raises(IssueFileConflict):
        issue_files.write(conn, layout, document())


def test_the_slug_cannot_change(conn, layout):
    issue_files.write(conn, layout, document())
    with pytest.raises(IssueFileError, match="简称"):
        issue_files.write(conn, layout, document(issue(slug="renamed")))


def test_invalid_frontmatter_is_not_written(conn, layout):
    with pytest.raises(IssueFileError, match=r"\$\.title"):
        issue_files.write(conn, layout, document(issue(title="")))
    assert not layout.issues_dir().exists()
    assert issues.get(conn, "0007") is None


def test_read_checks_the_file_name_and_id(layout):
    path = layout.issue_file("0009", "x")
    markdown.write(path, markdown.MarkdownDocument(issue_files.to_frontmatter(issue(), RUN), BODY))
    with pytest.raises(IssueFileError, match="与文件名不符"):
        issue_files.read(path)
    with pytest.raises(IssueFileError, match="文件名"):
        issue_files.read(layout.issues_dir() / "notes.md")


def test_reindex_rebuilds_from_files_only(conn, layout, tmp_path, clock):
    issue_files.write(conn, layout, document())
    issue_files.write(conn, layout, document(closed_issue("0012")))
    fresh = open_database(tmp_path / "fresh.db", clock)
    report = issue_files.reindex(fresh, layout)
    assert report == issue_files.ReindexReport(("0007", "0012"), ())
    assert issues.get(fresh, "0007") == issues.get(conn, "0007")
    assert sequences.next_issue_id(fresh) == "0013"
    assert issue_files.reindex(fresh, layout) == issue_files.ReindexReport((), ())
    layout.issue_file("0012", "duplicate").unlink()
    assert issue_files.reindex(fresh, layout).removed == ("0012",)
    fresh.close()


def test_reindex_lists_every_invalid_file_and_changes_nothing(conn, layout):
    issue_files.write(conn, layout, document())
    (layout.issues_dir() / "readme.md").write_text("# 说明\n", encoding="utf-8")
    (layout.issues_dir() / "0010-broken.md").write_text("---\nid: '0010'\n---\n", encoding="utf-8")
    layout.issue_file("0007", "order-owner-check").write_text("已损坏\n", encoding="utf-8")
    with pytest.raises(IssueFileError) as error:
        issue_files.reindex(conn, layout)
    message = str(error.value)
    assert "readme.md" in message and "0010-broken.md" in message and "0007-order-owner-check.md" in message
    assert issues.get(conn, "0007") is not None


def test_duplicate_ids_are_rejected(conn, layout):
    issue_files.write(conn, layout, document())
    text = layout.issue_file("0007", "order-owner-check").read_text(encoding="utf-8")
    layout.issue_file("0007", "copy").write_text(text, encoding="utf-8")
    with pytest.raises(IssueFileError, match="同时出现"):
        issue_files.reindex(conn, layout)


LEGACY = """---
type: issue
id: '0002'
title: 旧版式的 Issue
status: {status}
severity: P1
origin: triage
fixability: high
priorityScore: 4.0
problems:
- P-0042
rootCause:
- utils/monitor.py:148
triageCommit: 300e7a070abe23ff4caf05daddef8b77c9dbd8e6
findings: data/findings/P-0042.md
branch: null
pr: null
runId: R-20261001-070640-issue
createdAt: '2026-10-01T07:06:41Z'
updatedAt: '2026-10-01T10:01:12Z'
closeReason: {reason}
hold: null
github:
  number: 1
  url: https://github.com/owner/name/issues/1
dependsOn: null
---

# 旧版式的 Issue

## 问题

正文
"""


@pytest.mark.parametrize("status,reason,expected,phase", [
    ("needs-decision", "null", IssueStatus.NEEDS_DECISION, None),
    ("merged", "null", IssueStatus.DONE, "deploy-check"),
    ("closed", "wont-fix", IssueStatus.CANCELLED, None),
])
def test_legacy_files_are_read_with_the_new_statuses(layout, status, reason, expected, phase):
    path = layout.issue_file("0002", "legacy")
    path.parent.mkdir(parents=True)
    path.write_text(LEGACY.format(status=status, reason=reason), encoding="utf-8")
    found = issue_files.read(path).issue
    assert found.status is expected
    assert (found.phase.value if found.phase else None) == phase
    assert found.github.number == 1 and found.problems == ("P-0042",)


def test_upstream_is_the_parent_issue_or_the_first_problem():
    data = issue_files.to_frontmatter(issue(), RUN)
    assert (data["kind"], data["subject"], data["parent"], data["from"]) == ("issue", "0007", "P-0001", "triage")
    child = issue_files.to_frontmatter(replace(issue(), parent="0005", depends_on="0006"), RUN)
    assert (child["parent"], child["from"]) == ("0005", "fix")
    assert issue_files.from_frontmatter(child, "order-owner-check").issue.parent == "0005"
    assert issue_files.from_frontmatter(data, "order-owner-check").issue.parent is None
