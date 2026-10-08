"""已接受取舍的核对与知识的取用。

- 取证时把按位置匹配到的知识条目(knowledge.match，条数与 token 上限取 controls.knowledge)放进提示；位置是候选文件、
  问题的位置与它各条信号的位置(「方法 路由」、页面、`文件:行号`，由 knowledge.match.classify 分类后再匹配)；
- 模型给出的 tradeoffHit 由程序核对：编号在给出的条目中、类型是取舍、状态有效，三项都满足才判为已接受的取舍。
  防止模型编一个编号就吞掉问题。知识库把「已接受的取舍」归在约定与取舍(conventions)一类，没有再细分，
  所以「类型是取舍」按 conventions 判定。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tightrein.knowledge.entries import Entry, EntryStatus
from tightrein.knowledge.match import match, render

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime
    from tightrein.store.tables.occurrences import Occurrence
    from tightrein.store.tables.problems import Problem

TRADEOFF_KIND = "conventions"


def locations_of(problem: Problem, found: Sequence[Occurrence], files: Sequence[str]) -> list[str]:
    """匹配知识用的位置：调用方给的文件，加上问题与各条信号记下的位置(旧 retrieval/context.with_problem_locations)。"""
    values = [*files, problem.location, *(item.evidence.get("location") for item in found)]
    return list(dict.fromkeys(str(value) for value in values if value))


def entries_for(runtime: Runtime, paths: Sequence[str]) -> list[Entry]:
    limits = runtime.settings.section("knowledge")
    return match(runtime.workspace, list(paths), limit_entries=int(limits["matchEntries"]),
                 limit_tokens=int(limits["matchTokens"]))


def render_for(runtime: Runtime, entries: Sequence[Entry]) -> str:
    return render(entries, limit_tokens=int(runtime.settings.section("knowledge")["matchTokens"]))


def tradeoff_valid(hit: str | None, entries: Sequence[Entry]) -> bool:
    if hit is None:
        return False
    return any(entry.id == hit and entry.kind == TRADEOFF_KIND and entry.status == EntryStatus.ACTIVE
               for entry in entries)
