"""索引来源表(architecture/03 1.2)：扫描哪些路径、每个路径对应的类型与 frontmatter schema。

路径模式由 store.files.layout 的方法以通配符 `*` 作为编号与简称生成，本模块不另写目录名。
条目为 knowledge/<类型>/<编号>-<简称>.md；文档为 Issue、发现报告与修复报告。
INDEX.md 与分页文件 INDEX-<起始编号>-<结束编号>.md 由 index_md 生成，不建索引。
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from tightrein.domain.enums import KnowledgeType
from tightrein.store.files.layout import WorkspaceLayout

WILDCARD = "*"
ENTRY_SCHEMA = "data/knowledge.schema.json"
ISSUE_SCHEMA = "handoff/frontmatter/issue.schema.json"
REPORT_SCHEMA = "handoff/frontmatter/report.schema.json"
ISSUE = "issue"
FINDING = "finding"
FIX_REPORT = "fix-report"


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative: str
    type: str
    schema: str

    @property
    def is_entry(self) -> bool:
        return self.schema == ENTRY_SCHEMA


def _index_file(layout: WorkspaceLayout, kind: KnowledgeType, path: Path) -> bool:
    page = layout.knowledge_index_page(kind, WILDCARD, WILDCARD).name
    return path.name == layout.knowledge_index(kind).name or fnmatch(path.name, page)


def _fits(relative: str, pattern: str) -> bool:
    """fnmatch 的 `*` 也匹配 `/`，另要求层级相同。"""
    return fnmatch(relative, pattern) and relative.count("/") == pattern.count("/")


def _matches(layout: WorkspaceLayout, pattern: Path) -> list[Path]:
    return sorted(path for path in layout.root.glob(layout.relative(pattern)) if path.is_file())


def source_for(layout: WorkspaceLayout, path: Path) -> SourceFile | None:
    """路径属于哪一类来源；不在来源表中的返回空。"""
    relative = layout.relative(path)
    for kind in KnowledgeType:
        if _fits(relative, layout.relative(layout.knowledge_file(kind, WILDCARD, WILDCARD))):
            if _index_file(layout, kind, path):
                return None
            return SourceFile(path, relative, kind.value, ENTRY_SCHEMA)
    documents = (
        (layout.issue_file(WILDCARD, WILDCARD), ISSUE, ISSUE_SCHEMA),
        (layout.finding(WILDCARD), FINDING, REPORT_SCHEMA),
        (layout.fix_report(WILDCARD), FIX_REPORT, REPORT_SCHEMA),
    )
    for pattern, kind_name, schema in documents:
        if _fits(relative, layout.relative(pattern)):
            return SourceFile(path, relative, kind_name, schema)
    return None


def list_sources(layout: WorkspaceLayout) -> list[SourceFile]:
    patterns = [layout.knowledge_file(kind, WILDCARD, WILDCARD) for kind in KnowledgeType]
    patterns += [layout.issue_file(WILDCARD, WILDCARD), layout.finding(WILDCARD), layout.fix_report(WILDCARD)]
    found: dict[str, SourceFile] = {}
    for pattern in patterns:
        for path in _matches(layout, pattern):
            source = source_for(layout, path)
            if source is not None:
                found[source.relative] = source
    return [found[key] for key in sorted(found)]
