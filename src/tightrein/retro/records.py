"""复盘记录簿(retro/README.md「每条记录」)：每条一个 Markdown 文件，放在 data/retro/。

- 文件名 `<编号>-<评级>-<位置>-<英文短名>.md`，如 `0003-P1-implement.review-many-rounds.md`；评级变了就改名；
- 头信息(YAML)存全部数据，正文由程序按「结论 → 必填事实 → 每次出现」渲染给人看，只读头信息；
- 指纹 = 类型 + 阶段与小步骤 + 调用点 + 现象；调用点去掉末尾的序号后再算(同一调用点的多次调用并到一起)；
- 状态：待看(open)、已采纳(adopted)、已处理(done)、不处理(wontfix)。不处理的再出现只追加，不再提醒；
  已处理的再出现重新列为待看。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from tightrein.knowledge.entries import parse_frontmatter, render_frontmatter
from tightrein.protocol.naming import format_count, format_duration, retro_id, segment
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.markdown import write_markdown

CALL_SUFFIX = re.compile(r"-(\d+|[a-z]{2}-\d+)$")
FILE_NAME = re.compile(r"^(\d{4,})-P[0-3]-.+\.md$")
RATINGS = ("P0", "P1", "P2", "P3")


class Kind(StrEnum):
    FAILURE = "failure"
    WASTE = "waste"
    MISJUDGMENT = "misjudgment"
    INTERRUPTION = "interruption"


class Impact(IntEnum):
    """一次出现的影响大小，评级按它与出现次数计算。"""

    TRIVIAL = 0  # 体验、措辞、按设计的关卡
    MINOR = 1  # 偶发的浪费或失败
    MAJOR = 2  # 大量浪费、误判
    SEVERE = 3  # 整轮失败、卡死、需要用户救场


class RecordStatus(StrEnum):
    OPEN = "open"
    ADOPTED = "adopted"
    DONE = "done"
    WONTFIX = "wontfix"


KIND_LABELS = {Kind.FAILURE: "失败", Kind.WASTE: "浪费", Kind.MISJUDGMENT: "误判", Kind.INTERRUPTION: "打扰"}
STATUS_LABELS = {RecordStatus.OPEN: "待看", RecordStatus.ADOPTED: "已采纳", RecordStatus.DONE: "已处理",
                 RecordStatus.WONTFIX: "不处理"}


class RecordNotFound(KeyError):
    def __str__(self) -> str:
        return f"没有复盘记录 {self.args[0]}"


@dataclass(frozen=True)
class Occurrence:
    """一次运行中的出现：同一指纹的多条发现合成一段。"""

    run: str
    at: str
    count: int
    subjects: tuple[str, ...]
    details: tuple[str, ...]
    impact: Impact
    tokens: int = 0
    duration_ms: int = 0
    rounds: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"run": self.run, "at": self.at, "count": self.count, "subjects": list(self.subjects),
                "details": list(self.details), "impact": self.impact.name.lower(), "tokens": self.tokens,
                "durationMs": self.duration_ms, "rounds": self.rounds}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Occurrence:
        return cls(data["run"], data["at"], int(data["count"]), tuple(data["subjects"]), tuple(data["details"]),
                   Impact[str(data["impact"]).upper()], int(data["tokens"]), int(data["durationMs"]),
                   int(data["rounds"]))


@dataclass
class Record:
    id: str
    rating: str
    status: RecordStatus
    kind: Kind
    point: str  # 阶段与小步骤
    call_point: str  # 调用点(已去掉末尾序号)
    phenomenon: str  # 现象的英文短名，也是文件名的最后一段
    fact: str  # 现象：只写事实
    first_seen: str
    last_seen: str
    idea: str | None = None
    settle: str | None = None  # 建议沉淀为经验或规则(用户采纳后才做)
    occurrences: list[Occurrence] = field(default_factory=list)
    path: Path | None = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.kind, self.point, self.call_point, self.phenomenon)

    @property
    def count(self) -> int:
        return sum(item.count for item in self.occurrences)

    @property
    def subjects(self) -> set[str]:
        return {subject for item in self.occurrences for subject in item.subjects}

    @property
    def impact(self) -> Impact:
        return max((item.impact for item in self.occurrences), default=Impact.TRIVIAL)

    @property
    def tokens(self) -> int:
        return sum(item.tokens for item in self.occurrences)

    @property
    def duration_ms(self) -> int:
        return sum(item.duration_ms for item in self.occurrences)

    @property
    def rounds(self) -> int:
        return sum(item.rounds for item in self.occurrences)

    def file_name(self) -> str:
        return f"{self.id}-{self.rating}-{segment(self.point)}-{segment(self.phenomenon)}.md"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "rating": self.rating, "status": self.status.value, "kind": self.kind.value,
            "point": self.point, "callPoint": self.call_point, "phenomenon": self.phenomenon, "fact": self.fact,
            "firstSeen": self.first_seen, "lastSeen": self.last_seen, "count": self.count, "idea": self.idea,
            "settle": self.settle, "occurrences": [item.to_json() for item in self.occurrences],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], path: Path | None = None) -> Record:
        return cls(str(data["id"]), data["rating"], RecordStatus(data["status"]), Kind(data["kind"]), data["point"],
                   data["callPoint"], data["phenomenon"], data["fact"], data["firstSeen"], data["lastSeen"],
                   data.get("idea"), data.get("settle"),
                   [Occurrence.from_json(item) for item in data.get("occurrences") or []], path)

    def render(self) -> str:
        return render_frontmatter(self.to_json(), _body(self))


# 指纹


def call_point_group(point: str) -> str:
    """调用点归组：去掉末尾的序号与缺陷模式编号(`collect.static.verify-2` → `collect.static.verify`)。"""
    return CALL_SUFFIX.sub("", point)


def fingerprint(kind: Kind, point: str, call_point: str, phenomenon: str) -> str:
    return "|".join((kind.value, point, call_point_group(call_point), phenomenon))


# 读写


def load_all(layout: WorkspaceLayout) -> list[Record]:
    directory = layout.retro_dir
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.md")):
        if FILE_NAME.match(path.name):
            data, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            found.append(Record.from_json(data, path))
    return found


def get(layout: WorkspaceLayout, number: str) -> Record:
    wanted = retro_id(int(number))
    for record in load_all(layout):
        if record.id == wanted:
            return record
    raise RecordNotFound(number)


def next_id(records: Sequence[Record]) -> str:
    return retro_id(max((int(record.id) for record in records), default=0) + 1)


def save(layout: WorkspaceLayout, record: Record) -> Record:
    """写记录；评级变了文件名随之改，先写新文件再删旧文件。"""
    path = layout.retro_dir / record.file_name()
    write_markdown(path, record.render())
    if record.path is not None and record.path != path:
        record.path.unlink(missing_ok=True)
    record.path = path
    return record


def set_status(layout: WorkspaceLayout, number: str, status: RecordStatus) -> Record:
    """`tightrein retro close <编号> --done | --wontfix` 与采纳。"""
    record = get(layout, number)
    record.status = status
    return save(layout, record)


def listing(layout: WorkspaceLayout, statuses: Sequence[RecordStatus] = (RecordStatus.OPEN,)) -> list[Record]:
    """`tightrein retro list`：按评级、再按出现次数从多到少。"""
    chosen = [record for record in load_all(layout) if record.status in statuses]
    return sorted(chosen, key=lambda record: (record.rating, -record.count, record.id))


# 内部


def _body(record: Record) -> str:
    impact = [f"多花 token {format_count(record.tokens)}" if record.tokens else None,
              f"耗时 {format_duration(record.duration_ms / 1000)}" if record.duration_ms else None,
              f"返工 {record.rounds} 轮" if record.rounds else None]
    rows = [
        ("类型", KIND_LABELS[record.kind]),
        ("阶段与小步骤", f"`{record.point}`"),
        ("调用点", f"`{record.call_point}`"),
        ("现象", record.fact),
        ("影响(累计)", "，".join(item for item in impact if item) or "无"),
        ("解决思路", record.idea or "(未写)"),
        ("建议沉淀", record.settle or "无"),
    ]
    lines = [
        f"# {record.id} {KIND_LABELS[record.kind]}：{record.fact}",
        "",
        "## 结论",
        "",
        (f"评级 {record.rating}，状态 {STATUS_LABELS[record.status]}；出现 {record.count} 次，"
         f"首次 {record.first_seen}，最近 {record.last_seen}。"),
        "",
        "## 必填事实",
        "",
        "| 项 | 内容 |",
        "|---|---|",
        *(f"| {name} | {_cell(value)} |" for name, value in rows),
        "",
        "## 每次出现",
    ]
    for item in record.occurrences:
        lines += ["", f"### {item.run}({item.at}，{item.count} 次)", ""]
        lines += [f"- {detail}" for detail in item.details]
    return "\n".join(lines) + "\n"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
