"""知识条目的读写与格式校验(knowledge/README.md)。

- 每条一个 Markdown 文件：`knowledge/<类>/<编号>-<英文短名>.md`，头信息(YAML)在前，正文第一行是「# 标题」；
- 三类：conventions(约定与取舍)、patterns(缺陷模式)、lessons(经验)，编号前缀分别为 CON、PAT、LES；
- 头信息中的 locations 标明条目涉及的位置：`path:`(相对仓库根的文件或以 `/` 结尾的目录前缀)、
  `route:`(「方法 空格 路由模板」)、`page:`(以 `/` 开头的页面路由)；没有位置的条目对整个项目都适用；
- 条目从不删除文件：被推翻的标为 superseded 并写明 supersededBy；涉及的文件删除或大改时标为 stale(待确认)；
- 某个文件格式坏了不中断读取：沿用该文件上一次读好的内容并给出警告(上一次的结果缓存在 data/cache/)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from tightrein.store.files.json import read_json, write_json
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.markdown import parse_frontmatter, render_frontmatter, write_markdown

KINDS = ("conventions", "patterns", "lessons")
PREFIXES = {"conventions": "CON", "patterns": "PAT", "lessons": "LES"}
PATH_TAG = "path:"
ROUTE_TAG = "route:"
PAGE_TAG = "page:"
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ROUTE_VALUE = re.compile(rf"^({'|'.join(HTTP_METHODS)}) /\S*$")
PAGE_VALUE = re.compile(r"^/\S*$")
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ID = re.compile(r"^(CON|PAT|LES)-(\d{4,})$")
FILE_ID = re.compile(r"^(?:CON|PAT|LES)-(\d{4,})-")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FRONT = "---"
HEADING = re.compile(r"^# +(\S.*?)\s*$", re.MULTILINE)
REQUIRED = ("id", "kind", "summary", "status", "updated")
KNOWN_KEYS = frozenset({*REQUIRED, "locations", "commit", "sources", "supersededBy", "staleReason"})


class EntryStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"  # 待确认：涉及的文件删除或大改
    SUPERSEDED = "superseded"


class EntryInvalid(ValueError):
    """条目格式不合格；problems 每条带文件、字段与原因，一次列全。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    title: str
    summary: str
    body: str  # 标题行之后的正文
    status: EntryStatus = EntryStatus.ACTIVE
    updated: str = ""  # 本地日期 YYYY-MM-DD
    locations: tuple[str, ...] = ()
    commit: str | None = None  # 写入时项目的 commit：过期比对的基准
    sources: tuple[str, ...] = ()  # 提出它的对象编号(Issue、问题)
    superseded_by: str | None = None
    stale_reason: str | None = None
    path: Path | None = field(default=None, compare=False)

    def to_json(self) -> dict[str, Any]:
        """头信息：不适用的字段写 null，不省略。"""
        return {
            "id": self.id, "kind": self.kind, "summary": self.summary, "status": self.status.value,
            "updated": self.updated, "locations": list(self.locations), "commit": self.commit,
            "sources": list(self.sources), "supersededBy": self.superseded_by, "staleReason": self.stale_reason,
        }

    def render(self) -> str:
        return render_frontmatter(self.to_json(), f"# {self.title}\n\n{self.body.strip()}\n")


@dataclass(frozen=True)
class Loaded:
    entries: list[Entry]
    warnings: list[str]


# 头信息


# 校验


def location_problem(location: str) -> str | None:
    """位置标签的格式问题；合格时为 None。"""
    if location.startswith(PATH_TAG):
        value = location[len(PATH_TAG):]
        if (not value or value.startswith("/") or "\\" in value or any(char.isspace() for char in value)
                or ".." in value.split("/")):
            return "path: 须为仓库内的相对路径或以 / 结尾的目录前缀，不以 / 开头，不含 ..、反斜杠与空白"
        return None
    if location.startswith(ROUTE_TAG):
        if not ROUTE_VALUE.match(location[len(ROUTE_TAG):]):
            return "route: 须为「HTTP 方法 空格 以 / 开头的路由模板」，例如 route:POST /api/orders"
        return None
    if location.startswith(PAGE_TAG):
        return None if PAGE_VALUE.match(location[len(PAGE_TAG):]) else "page: 须为以 / 开头的前端页面路由"
    return f"位置须以 {PATH_TAG}、{ROUTE_TAG} 或 {PAGE_TAG} 开头"


def problems(data: dict[str, Any], body: str, *, file: str, kind: str | None = None) -> list[str]:
    """头信息与正文的全部格式问题，每条为「文件：字段：原因」。kind 为所在目录的类。"""
    found = [f"{file}：{key}：缺少必填项" for key in REQUIRED if data.get(key) in (None, "")]
    found += [f"{file}：{key}：不认识的字段" for key in data if key not in KNOWN_KEYS]
    entry_kind = None if data.get("kind") is None else str(data["kind"])
    if entry_kind is not None and entry_kind not in KINDS:
        found.append(f"{file}：kind：只能是 {'、'.join(KINDS)}")
    if kind is not None and entry_kind is not None and entry_kind != kind:
        found.append(f"{file}：kind：位于 {kind} 目录，kind 却是 {entry_kind}")
    entry_id = data.get("id")
    if entry_id is not None:
        matched = ID.match(str(entry_id))
        if matched is None:
            found.append(f"{file}：id：须为「前缀-四位以上数字」，前缀为 {'、'.join(PREFIXES.values())}")
        elif entry_kind in PREFIXES and matched.group(1) != PREFIXES[entry_kind]:
            found.append(f"{file}：id：{entry_id} 的前缀与类 {entry_kind} 不符，应为 {PREFIXES[entry_kind]}")
        if file != "<草稿>" and not Path(file).name.startswith(f"{entry_id}-"):
            found.append(f"{file}：id：文件名须以「{entry_id}-」开头")
    status = None if data.get("status") is None else str(data["status"])
    if status is not None and status not in {item.value for item in EntryStatus}:
        found.append(f"{file}：status：只能是 {'、'.join(item.value for item in EntryStatus)}")
    if (status == EntryStatus.SUPERSEDED) != (data.get("supersededBy") is not None):
        found.append(f"{file}：supersededBy：只有已取代的条目写明 supersededBy，且必须写明")
    if data.get("updated") is not None and not DATE.match(str(data["updated"])):
        found.append(f"{file}：updated：须为 YYYY-MM-DD")
    locations = data.get("locations") or []
    if not isinstance(locations, list):
        found.append(f"{file}：locations：须为列表")
        locations = []
    for index, location in enumerate(locations):
        reason = location_problem(str(location))
        if reason is not None:
            found.append(f"{file}：locations[{index}]：{reason}")
    if HEADING.search(body) is None:
        found.append(f"{file}：正文：没有一级标题(# 开头的行)")
    return found


# 读写


def read_entry(path: Path, kind: str | None = None) -> Entry:
    text = path.read_text(encoding="utf-8")
    try:
        data, body = parse_frontmatter(text)
    except ValueError as error:
        raise EntryInvalid([f"{path}：头信息：{error}"]) from error
    found = problems(data, body, file=str(path), kind=kind)
    if found:
        raise EntryInvalid(found)
    return _entry(data, body, path)


def save(entry: Entry) -> Entry:
    """写回条目文件(原子写，不留半截内容)。"""
    if entry.path is None:
        raise ValueError(f"{entry.id} 没有文件路径")
    write_markdown(entry.path, entry.render())
    return entry


def create(layout: WorkspaceLayout, *, kind: str, slug: str, title: str, summary: str, body: str,
           locations: tuple[str, ...], today: str, commit: str | None, sources: tuple[str, ...]) -> Entry:
    """分配编号并写新条目；先校验，不合格时抛 EntryInvalid，不写文件。"""
    if kind not in PREFIXES:
        raise EntryInvalid([f"<草稿>：kind：只能是 {'、'.join(KINDS)}"])
    found = [] if SLUG.match(slug) else [f"<草稿>：slug：只能由小写字母、数字与连字符组成：{slug!r}"]
    entry_id = next_id(layout, kind)
    entry = Entry(entry_id, kind, title.strip(), summary.strip(), body.strip(), EntryStatus.ACTIVE, today,
                  tuple(locations), commit, tuple(sources))
    found += problems(entry.to_json(), f"# {entry.title}\n", file="<草稿>")
    found += [f"<草稿>：{name}：不能为空" for name in ("title", "body") if not getattr(entry, name)]
    if found:
        raise EntryInvalid(found)
    return save(replace(entry, path=layout.knowledge_kind(kind) / f"{entry_id}-{slug}.md"))


def supersede(entry: Entry, by: str, today: str) -> Entry:
    return save(replace(entry, status=EntryStatus.SUPERSEDED, superseded_by=by, stale_reason=None, updated=today))


def mark_stale(entry: Entry, reason: str, today: str) -> Entry:
    return save(replace(entry, status=EntryStatus.STALE, stale_reason=reason, updated=today))


def load(layout: WorkspaceLayout) -> Loaded:
    """读出全部条目。格式坏了的文件沿用上一次读好的内容(没有时跳过)，并给出警告。"""
    cache_path = layout.knowledge_cache
    cache: dict[str, Any] = read_json(cache_path) if cache_path.is_file() else {}
    fresh: dict[str, Any] = {}
    entries: list[Entry] = []
    warnings: list[str] = []
    for kind in KINDS:
        directory = layout.knowledge_kind(kind)
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            relative = path.relative_to(layout.root).as_posix()
            try:
                entry = read_entry(path, kind)
            except EntryInvalid as error:
                kept = cache.get(relative)
                warnings.append(f"{'；'.join(error.problems)}({'沿用上一次读好的内容' if kept else '跳过'})")
                if kept is not None:
                    fresh[relative] = kept
                    entries.append(_entry(kept["data"], kept["body"], path))
                continue
            fresh[relative] = {"data": entry.to_json(), "body": f"# {entry.title}\n\n{entry.body}\n"}
            entries.append(entry)
    if fresh != cache:
        write_json(cache_path, fresh)
    return Loaded(entries, warnings)


def active(entries: list[Entry]) -> list[Entry]:
    return [entry for entry in entries if entry.status is EntryStatus.ACTIVE]


def next_id(layout: WorkspaceLayout, kind: str) -> str:
    """该类已用的最大编号加一；编号来自文件名，文件是唯一来源。"""
    prefix = PREFIXES[kind]
    directory = layout.knowledge_kind(kind)
    used = [int(match.group(1)) for path in (directory.glob(f"{prefix}-*.md") if directory.is_dir() else [])
            if (match := FILE_ID.match(path.name)) is not None]
    return f"{prefix}-{max(used, default=0) + 1:04d}"


def find(entries: list[Entry], entry_id: str) -> Entry | None:
    return next((entry for entry in entries if entry.id == entry_id), None)


# 内部


def _entry(data: dict[str, Any], body: str, path: Path | None) -> Entry:
    heading = HEADING.search(body)
    title = heading.group(1) if heading else ""
    rest = body[heading.end():].strip() if heading else body.strip()
    return Entry(
        id=str(data["id"]), kind=str(data["kind"]), title=title, summary=str(data["summary"]), body=rest,
        status=EntryStatus(data["status"]), updated=str(data["updated"]),
        locations=tuple(str(item) for item in data.get("locations") or ()), commit=data.get("commit"),
        sources=tuple(str(item) for item in data.get("sources") or ()), superseded_by=data.get("supersededBy"),
        stale_reason=data.get("staleReason"), path=path,
    )
