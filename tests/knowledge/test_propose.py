from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.agents.params import CallParams
from tightrein.agents.result import CallResult, CallStatus
from tightrein.assess import notes as code_notes
from tightrein.assess.notes import CodeNotes
from tightrein.knowledge import entries, propose
from tightrein.knowledge.entries import EntryStatus
from tightrein.knowledge.propose import Content, Decision, DecisionKind, ProposalError, ProposalStatus
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.naming import FixedClock
from tightrein.settings.load import Settings
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-10-08T01:00:00Z"
RUN = "R-20261008T010000Z-run"


def content(slug: str = "missing-filter", locations: tuple[str, ...] = ("path:src/services/",)) -> Content:
    return Content("patterns", slug, "列表接口漏了公司过滤", "按编号查询时没有按公司过滤", locations, "服务层直接按编号查询。")


def existing(layout: WorkspaceLayout, slug: str) -> str:
    return entries.create(layout, kind="patterns", slug=slug, title="旧条目", summary="旧摘要", body="旧正文。",
                          locations=("path:src/services/",), today="2026-10-01", commit="a" * 40,
                          sources=("0003",)).id


# 收集与去重


def test_suggestions_are_read_from_the_handoff_facts() -> None:
    handoff = Handoff("implement.review", "0018", RUN, Status.PASSED, "通过",
                      {"knowledgeSuggestions": ["  分页参数要校验  ", ""]})
    assert propose.suggestions_in(handoff) == ["分页参数要校验"]
    assert propose.suggestions_in(Handoff("implement.review", "0018", RUN, Status.PASSED, "通过", {})) == []


def test_the_same_suggestion_is_listed_once_and_collects_subjects(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    listed = propose.add(layout, "0018", ["分页参数要校验。", "导出要异步"], NOW)
    assert [item.id for item in listed] == ["0001", "0002"]
    assert propose.add(layout, "0019", ["分页参数  要校验"], NOW) == []
    assert propose.add(layout, "0018", ["分页参数要校验"], NOW) == []
    first, second = propose.pending(layout)
    assert (first.subjects, second.subjects) == (["0018", "0019"], ["0018"])


def test_a_rejected_suggestion_returns_only_with_a_new_subject(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    propose.add(layout, "0018", ["分页参数要校验"], NOW)
    rejected = propose.reject(layout, "0001", "已在约定里写过", NOW)
    assert rejected.status is ProposalStatus.REJECTED and rejected.rejected_subjects == ["0018"]
    assert propose.add(layout, "0018", ["分页参数要校验"], NOW) == []
    assert propose.pending(layout) == []
    (again,) = propose.add(layout, "0020", ["分页参数要校验"], NOW)
    assert (again.id, again.status, again.subjects) == ("0001", ProposalStatus.PENDING, ["0018", "0020"])
    with pytest.raises(ProposalError, match="不存在"):
        propose.reject(layout, "0009", "x", NOW)


# 判断的校验


@pytest.mark.parametrize("decision, reason", [
    (Decision(DecisionKind.UPDATE, ("PAT-0001", "PAT-0002"), (), content(), "r"), "恰好 1 个"),
    (Decision(DecisionKind.MERGE, (), (), content(), "r"), "至少 1 个"),
    (Decision(DecisionKind.NOOP, (), (), None, "r"), "至少 1 个"),
    (Decision(DecisionKind.ADD, ("PAT-0001",), (), content(), "r"), "targetIds 须为空"),
    (Decision(DecisionKind.UPDATE, ("PAT-0001",), ("PAT-0002",), content(), "r"), "只有 add"),
    (Decision(DecisionKind.ADD, (), ("PAT-0009",), content(), "r"), "不在候选条目中"),
    (Decision(DecisionKind.ADD, (), (), None, "r"), "完整条目"),
    (Decision(DecisionKind.ADD, (), (), content(slug="Bad"), "r"), "entry.slug"),
    (Decision(DecisionKind.ADD, (), (), content(locations=("route:x",)), "r"), "entry.locations[0]"),
])
def test_a_semantically_invalid_decision_is_rejected(decision: Decision, reason: str) -> None:
    assert any(reason in item for item in propose.check_decision(decision, ["PAT-0001", "PAT-0002"]))


def test_a_valid_decision_has_no_reasons() -> None:
    assert propose.check_decision(Decision(DecisionKind.NOOP, ("PAT-0001",), (), None, "r"), ["PAT-0001"]) == []
    assert propose.check_decision(Decision(DecisionKind.ADD, (), ("PAT-0001",), content(), "r"), ["PAT-0001"]) == []


# 写入


def test_noop_writes_nothing(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    old = existing(layout, "old")
    before = sorted(path.read_text(encoding="utf-8") for path in layout.knowledge_dir.rglob("*.md"))
    loaded = entries.load(layout).entries
    written = propose.apply(layout, Decision(DecisionKind.NOOP, (old,), (), None, "已覆盖"), loaded, ["0018"],
                            today="2026-10-08", commit="b" * 40)
    assert written == old
    assert sorted(path.read_text(encoding="utf-8") for path in layout.knowledge_dir.rglob("*.md")) == before


def test_merge_writes_a_new_entry_and_supersedes_the_targets(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    first, second = existing(layout, "first"), existing(layout, "second")
    loaded = entries.load(layout).entries
    written = propose.apply(layout, Decision(DecisionKind.MERGE, (first, second), (), content(), "同一根因"), loaded,
                            ["0018"], today="2026-10-08", commit="b" * 40)
    after = {entry.id: entry for entry in entries.load(layout).entries}
    assert written == "PAT-0003" and after[written].sources == ("0018",) and after[written].commit == "b" * 40
    assert after[first].status is after[second].status is EntryStatus.SUPERSEDED
    assert after[first].superseded_by == after[second].superseded_by == written


def test_add_supersedes_old_entries_and_update_rewrites_in_place(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    old = existing(layout, "old")
    added = propose.apply(layout, Decision(DecisionKind.ADD, (), (old,), content(), "推翻"),
                          entries.load(layout).entries, ["0018"], today="2026-10-08", commit=None)
    after = {entry.id: entry for entry in entries.load(layout).entries}
    assert after[old].superseded_by == added
    updated = propose.apply(layout, Decision(DecisionKind.UPDATE, (added,), (), content(slug="ignored"), "补充"),
                            entries.active(entries.load(layout).entries), ["0019"], today="2026-10-09",
                            commit="c" * 40)
    entry = {item.id: item for item in entries.load(layout).entries}[updated]
    assert updated == added and entry.sources == ("0018", "0019") and entry.updated == "2026-10-09"
    assert entry.path is not None and entry.path.name == f"{added}-missing-filter.md"


# 确认后写入(模型由假实现代替)


@dataclass
class FakeInvoke:
    outputs: list[dict[str, Any]]
    prompts: list[str] = field(default_factory=list)

    def __call__(self, params: CallParams, context: object) -> CallResult:
        self.prompts.append(params.prompt)
        assert params.point == "knowledge.curate"
        return CallResult(CallStatus.OK, "claude", "opus", output=self.outputs.pop(0))


def runtime(tmp_path: Path) -> SimpleNamespace:
    settings = Settings.load(ToolLayout(REPO_ROOT))
    return SimpleNamespace(
        workspace=WorkspaceLayout(tmp_path), settings=settings, run=RUN, agents=object(), language="zh",
        clock=FixedClock(datetime(2026, 10, 8, 1, 0, tzinfo=UTC)),
        git=SimpleNamespace(head=lambda: SimpleNamespace(commit="d" * 40, branch="main")),
    )


def output(decision: str, targets: list[str], entry: dict[str, Any] | None) -> dict[str, Any]:
    return {"analysis": "a", "decision": decision, "targetIds": targets, "supersedes": [], "entry": entry,
            "reason": "r"}


ENTRY = {"kind": "lessons", "slug": "page-size", "title": "分页参数要校验", "summary": "负数与过大都要拦",
         "locations": ["path:src/api/"], "body": "列表接口先校验分页参数。"}


def test_accept_retries_an_invalid_decision_once_with_the_reasons(tmp_path: Path) -> None:
    context = runtime(tmp_path)
    layout = context.workspace
    old = existing(layout, "old")
    code_notes.save(layout, CodeNotes(subject="0018", commit="a" * 40, entries=[], files=["src/services/order.py"],
                                      trigger=None))
    propose.add(layout, "0018", ["分页参数要校验"], NOW)
    invoke = FakeInvoke([output("update", [old, "PAT-0009"], ENTRY), output("add", [], ENTRY)])
    accepted = propose.accept(context, "0001", invoke=invoke)  # type: ignore[arg-type]
    assert accepted.status is ProposalStatus.ACCEPTED and accepted.entry == "LES-0001"
    assert old in invoke.prompts[0] and "需要处理的问题" in invoke.prompts[1] and "PAT-0009" in invoke.prompts[1]
    written = {entry.id: entry for entry in entries.load(layout).entries}["LES-0001"]
    assert written.commit == "d" * 40 and written.sources == ("0018",)


def test_a_decision_invalid_twice_is_not_written(tmp_path: Path) -> None:
    context = runtime(tmp_path)
    layout = context.workspace
    propose.add(layout, "0018", ["分页参数要校验"], NOW)
    invoke = FakeInvoke([output("noop", [], None), output("merge", [], ENTRY)])
    with pytest.raises(ProposalError, match="两次判断都不合格"):
        propose.accept(context, "0001", invoke=invoke)  # type: ignore[arg-type]
    assert not list(layout.knowledge_dir.rglob("*.md"))
    assert propose.pending(layout)[0].id == "0001"
