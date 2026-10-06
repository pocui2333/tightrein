"""INDEX.md 的生成与分页(architecture/03 1.6.4，design 16.4)。

- `knowledge/<类型>/INDEX.md`：该类型 active 的条目，每行 `- <编号> <摘要> (<文件名>)`，按编号排序；
- `knowledge/INDEX.md`：每个类型一节，列出条目数与该类型 INDEX.md 的相对链接；全部类型的条目合计不超过 rootListingLimit(默认 60)条时
  直接列出各条目，否则只给链接；
- 内容只取自 knowledge_meta，不读文件；第一行写明由 tightrein admin kb sync 生成；
- 每个文件不超过 indexMaxLines(默认 200)行：某一类型超出时，该类型的 INDEX.md 只列出分页文件，条目按编号每
  indexPageSize(默认 180)条一页写入
  `INDEX-<起始编号>-<结束编号>.md`，不再需要的分页文件删除；
- 生成结果与磁盘上相同时不写文件；superseded 与 archived 的条目不进入 INDEX.md。
只为已有目录的类型生成该类型的 INDEX.md。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from tightrein.config import layers
from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.retrieval.sources import WILDCARD
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import knowledge
from tightrein.store.repos.knowledge import KnowledgeRecord

GENERATED_NOTICE = "<!-- 本文件由 tightrein admin kb sync 生成，不要手工编辑 -->"


def _heading(kind: KnowledgeType) -> str:
    return f"{kind.label}({kind.value})"


def _line(record: KnowledgeRecord, name: str) -> str:
    return f"- {record.id} {record.summary} ({name})"


def _document(title: str, lines: list[str]) -> str:
    return "\n".join([GENERATED_NOTICE, f"# {title}", "", *lines]) + "\n"


@dataclass(frozen=True)
class Paging:
    """INDEX 文件的分页(runtime.retrieval.indexMaxLines、indexPageSize、rootListingLimit)；缺省取核心缺省值。"""

    max_lines: int = field(default_factory=lambda: int(layers.core_value("runtime.retrieval.indexMaxLines")))
    page_size: int = field(default_factory=lambda: int(layers.core_value("runtime.retrieval.indexPageSize")))
    root_listing_limit: int = field(
        default_factory=lambda: int(layers.core_value("runtime.retrieval.rootListingLimit")))


def _pages(records: list[KnowledgeRecord], size: int) -> list[list[KnowledgeRecord]]:
    return [records[start:start + size] for start in range(0, len(records), size)]


def render(conn: sqlite3.Connection, layout: WorkspaceLayout, paging: Paging | None = None) -> dict[Path, str]:
    """全部 INDEX 文件的内容，键为绝对路径。"""
    paging = paging or Paging()
    active = knowledge.find(conn, status=KnowledgeStatus.ACTIVE)
    files: dict[Path, str] = {}
    sections: list[str] = []
    listed_kinds = [kind for kind in KnowledgeType if layout.knowledge_type_dir(kind).is_dir()]
    by_kind = {kind: [record for record in active if record.type == kind.value] for kind in listed_kinds}
    total = sum(len(records) for records in by_kind.values())
    for kind, records in by_kind.items():
        lines = [_line(record, Path(record.path).name) for record in records] or ["暂无有效条目。"]
        if len(lines) + 3 > paging.max_lines:
            lines = []
            for page in _pages(records, paging.page_size):
                path = layout.knowledge_index_page(kind, page[0].id, page[-1].id)
                files[path] = _document(f"{_heading(kind)} {page[0].id} 到 {page[-1].id}",
                                        [_line(record, Path(record.path).name) for record in page])
                lines.append(f"- [{page[0].id} 到 {page[-1].id}]({path.name})")
        files[layout.knowledge_index(kind)] = _document(_heading(kind), lines)
        link = layout.knowledge_index(kind).relative_to(layout.knowledge_dir()).as_posix()
        sections += [f"## {_heading(kind)}", "", f"共 {len(records)} 条，见 [{link}]({link})", ""]
        if total <= paging.root_listing_limit and records:
            sections += [_line(record, (layout.root / record.path).relative_to(layout.knowledge_dir()).as_posix())
                         for record in records]
            sections.append("")
    files[layout.knowledge_index()] = _document("知识索引", sections[:-1] if sections else ["暂无条目。"])
    return files


def regenerate(conn: sqlite3.Connection, layout: WorkspaceLayout, paging: Paging | None = None) -> list[str]:
    """写入内容有变化的 INDEX 文件、删除不再需要的分页文件，返回改写或删除的文件(相对工作区)。"""
    files = render(conn, layout, paging)
    changed: list[str] = []
    for path, text in sorted(files.items()):
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            continue
        atomic.write_text(path, text)
        changed.append(layout.relative(path))
    for kind in KnowledgeType:
        directory = layout.knowledge_type_dir(kind)
        if not directory.is_dir():
            continue
        pattern = layout.knowledge_index_page(kind, WILDCARD, WILDCARD).name
        for page in sorted(directory.iterdir()):
            if fnmatch(page.name, pattern) and page not in files:
                page.unlink()
                changed.append(layout.relative(page))
    return sorted(changed)
