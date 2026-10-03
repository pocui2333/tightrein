"""读取来源文件的 frontmatter、按 schema 校验、检查标签前缀，取出索引字段(architecture/03 1.2)。

| 索引字段 | 条目 | Issue | 报告与提案 |
|---|---|---|---|
| 标题 | 正文第一个一级标题 | frontmatter title | 正文第一个一级标题 |
| 摘要 | summary | title | summary |
| 标签 | tags | rootCause 的文件生成 path:，关联问题的路由生成 route: | tags |
| 更新日期 | updated | updated 的 UTC 日期(旧版式的头信息先换算) | updatedAt，没有时 createdAt 的 UTC 日期 |

条目另检查：frontmatter 的 type 与所在目录一致、文件名以「编号-」开头；Issue 的文件名以「编号-」开头；
报告的 type 与所在目录一致。文档的 status 在索引中固定为 active，related 与 supersededBy 为空。
每个问题带文件路径、JSON 路径与原因，调用方汇总全部问题后决定是否写入。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from tightrein.contracts import validate
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import KnowledgeStatus, Stage
from tightrein.retrieval.errors import FrontmatterIssue
from tightrein.retrieval.sources import ISSUE, SourceFile
from tightrein.store.files import issue_files, markdown

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
PATH_TAG = "path:"
ROUTE_TAG = "route:"
PAGE_TAG = "page:"
STAGE_TAG = "stage:"
ROUTE_VALUE = re.compile(rf"^({'|'.join(HTTP_METHODS)}) /\S*$")
PAGE_VALUE = re.compile(r"^/\S*$")
HEADING = re.compile(r"^# +(\S.*?)\s*$", re.MULTILINE)
LOCATION_LINE = re.compile(r":\d+(-\d+)?$")

RoutesOf = Callable[[Sequence[str]], list[str]]


@dataclass(frozen=True)
class IndexedEntry:
    """一个来源文件解析后的全部索引字段与正文。"""

    id: str
    type: str
    status: KnowledgeStatus
    title: str
    summary: str
    tags: tuple[str, ...]
    related: tuple[str, ...]
    superseded_by: str | None
    updated: date
    review_by: date | None
    path: str
    body: str
    frontmatter: Mapping[str, Any]


def tag_problem(tag: str) -> str | None:
    """有固定含义的前缀的格式检查；自由文本标签不检查。"""
    if tag.startswith(PATH_TAG):
        value = tag[len(PATH_TAG):]
        parts = value.split("/")
        if not value or value.startswith("/") or "\\" in value or any(char.isspace() for char in value) \
                or ".." in parts:
            return "path: 须为仓库内的相对路径或目录前缀，不以 / 开头，不含 ..、反斜杠与空白"
    elif tag.startswith(ROUTE_TAG):
        if not ROUTE_VALUE.match(tag[len(ROUTE_TAG):]):
            return "route: 须为「HTTP 方法 空格 以 / 开头的路由模板」，例如 route:POST /api/Material/Query"
    elif tag.startswith(PAGE_TAG):
        if not PAGE_VALUE.match(tag[len(PAGE_TAG):]):
            return "page: 须为以 / 开头的前端页面路由"
    elif tag.startswith(STAGE_TAG):
        if tag[len(STAGE_TAG):] not in {stage.value for stage in Stage}:
            return "stage: 须为本工具的环节之一：" + "、".join(stage.value for stage in Stage)
    return None


def tag_issues(tags: Sequence[str], pointer: str = "$.tags") -> list[tuple[str, str]]:
    """返回 (JSON 路径, 原因)。"""
    found = []
    for index, tag in enumerate(tags):
        problem = tag_problem(tag)
        if problem is not None:
            found.append((f"{pointer}[{index}]", problem))
    return found


def first_heading(body: str) -> str | None:
    match = HEADING.search(body)
    return None if match is None else match.group(1)


def location_file(location: str) -> str:
    """`文件:行号` 或 `文件:起-止` 中的文件部分。"""
    return LOCATION_LINE.sub("", location)


def _utc_date(text: str) -> date:
    return parse_iso(text).date()


def parse_source(source: SourceFile, text: str,
                 routes_of: RoutesOf) -> tuple[IndexedEntry | None, list[FrontmatterIssue]]:
    """解析一个来源文件；有问题时第一项为空。"""

    def issue(pointer: str, reason: str) -> FrontmatterIssue:
        return FrontmatterIssue(source.relative, pointer, reason)

    try:
        document = markdown.parse(text, source.relative)
    except markdown.FrontmatterError as error:
        return None, [issue("$", str(error))]
    data = document.frontmatter
    if source.type == ISSUE and data.get("type") == issue_files.LEGACY_TYPE:
        data = issue_files.from_legacy(data)
    problems = [issue(error.path, error.reason) for error in validate.validate(source.schema, data)]
    if problems:
        return None, problems
    problems += [issue(pointer, reason) for pointer, reason in tag_issues(data.get("tags", ()))]
    name = source.path.name
    heading = first_heading(document.body)
    if source.type == ISSUE:
        if not name.startswith(f"{data['id']}-"):
            problems.append(issue("$.id", f"文件名须以「{data['id']}-」开头"))
        paths = sorted({location_file(location) for location in data["rootCause"]})
        tags = tuple([f"{PATH_TAG}{path}" for path in paths]
                     + [f"{ROUTE_TAG}{route}" for route in routes_of(data["problems"])])
        title = summary = data["title"]
        updated = _utc_date(data["updated"])
    else:
        if data["type"] != source.type:
            problems.append(issue("$.type", f"位于 {source.type} 的目录，type 却是 {data['type']}"))
        if source.is_entry and not name.startswith(f"{data['id']}-"):
            problems.append(issue("$.id", f"文件名须以「{data['id']}-」开头"))
        if heading is None:
            problems.append(issue("$", "正文没有一级标题(# 开头的行)，无法取得标题"))
        tags = tuple(data["tags"])
        title = heading or ""
        summary = data["summary"]
        updated = (date.fromisoformat(data["updated"]) if source.is_entry
                   else _utc_date(data.get("updatedAt") or data["createdAt"]))
    if problems:
        return None, problems
    entry = IndexedEntry(
        id=data["id"], type=source.type,
        status=KnowledgeStatus(data["status"]) if source.is_entry else KnowledgeStatus.ACTIVE,
        title=title, summary=summary, tags=tags,
        related=tuple(data.get("related", ())) if source.is_entry else (),
        superseded_by=data.get("supersededBy") if source.is_entry else None,
        updated=updated,
        review_by=date.fromisoformat(data["reviewBy"]) if source.is_entry else None,
        path=source.relative, body=document.body, frontmatter=data,
    )
    return entry, []
