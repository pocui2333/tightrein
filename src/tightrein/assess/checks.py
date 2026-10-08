"""证据检查：全部由程序判定，不另设模型评审(原评分表 evaluation/rubrics/triage.json 与 scorers/code.py)。

- 位置补全：取证输出中的位置应是相对仓库根的完整路径；只写了文件名或缺了前几级目录时，按代码快照中「唯一的路径
  后缀」补全，零个或多个匹配就不补，留给检查判不通过；文字中引用的 `文件:行号` 也补，补不全的只在扩展名在快照中
  出现过时才报(不把普通文字当代码引用)；只去掉开头的 `./`(lstrip 会把 `.github`、`.eslintrc.js` 削掉)。
- 检查项：每条证据都是 `文件:行号` 且真实存在、不越界；判定是四档之一，证据不足写缺什么、条件成立写条件；
  判不成立时挡住它的代码(location)与主张中第几条事实(factRef)恰好给一个；反证至少一条、追到入口(原样引用主张中的
  入口或真实存在的位置)，写「上游有校验」时给出并核对位置；结论不含含糊措辞；评估(assessment)在判成立时必须给出，
  预估要改的已有文件真实存在、新建的标 isNew，建议暂不修时写重估条件。
返回的每条原因带检查项编号，交回同一角色重做。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERDICTS = ("confirmed", "conditional", "refuted", "insufficient")
CONFIRMING = frozenset({"confirmed", "conditional"})
# 文字中引用的 `文件:行号`：文件名带扩展名，前面不是路径字符
IN_TEXT = re.compile(r"(?<![\w/.-])([\w./-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")
WHOLE = re.compile(r"([^:\s][^:]*):(\d+)(?:-(\d+))?")
SKIPPED_DIRECTORIES = frozenset({".git", "node_modules"})
FILE_KEY = "file"
LINE_KEY = "line"
# 要核对的证据位置：(取值路径, 说明)；路径中 [*] 展开列表
EVIDENCE_PATHS = ("facts[*].location", "counterEvidence[*].upstreamValidation.location",
                  "sourceOfPhenomenon.location", "rootCauses[*]", "impact.callSites[*]")
VAGUE_FIELDS = ("facts[*].observation", "trigger", "counterEvidence[*].result")


@dataclass
class Snapshot:
    """代码快照(只读 worktree)中的文件清单；第一次需要补全时才遍历，同一次检查内复用。"""

    root: Path
    _files: list[str] | None = None
    _lines: dict[str, int] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    def files(self) -> list[str]:
        if self._files is None:
            found = []
            for directory, names, files in os.walk(self.root):
                names[:] = [name for name in names if name not in SKIPPED_DIRECTORIES]
                base = Path(directory).relative_to(self.root)
                found += [(base / name).as_posix() for name in files]
            self._files = found
        return self._files

    def exists(self, file: str) -> bool:
        path = (self.root / file).resolve()
        return self.root.resolve() in path.parents and path.is_file()

    def line_count(self, file: str) -> int:
        if file not in self._lines:
            text = (self.root / file).read_text(encoding="utf-8", errors="replace")
            self._lines[file] = len(text.splitlines())
        return self._lines[file]

    def resolve(self, file: str) -> str | None:
        """完整路径：去掉开头的 `./` 后已存在时返回，唯一后缀匹配时补全，否则为 None。"""
        file = re.sub(r"^(\./)+", "", file)
        if self.exists(file):
            return file
        suffix = "/" + file
        found = [path for path in self.files() if ("/" + path).endswith(suffix)]
        return found[0] if len(found) == 1 else None

    def is_code_extension(self, file: str) -> bool:
        extension = Path(file).suffix
        return bool(extension) and any(path.endswith(extension) for path in self.files())

    def problem(self, value: Any) -> str | None:
        """value 不是「文件:行号」、文件不存在或行号越界时的原因。"""
        location = parse_location(value)
        if location is None:
            return f"{value!r} 不是「文件路径:行号」"
        file, _, last = location
        if not self.exists(file):
            return f"{file} 在代码中不存在"
        count = self.line_count(file)
        if last > count:
            return f"{file} 只有 {count} 行，引用了第 {last} 行"
        return None


@dataclass(frozen=True)
class Completed:
    value: Any
    unresolved: tuple[str, ...]

    def problems(self) -> list[str]:
        return [f"[locations] 文字中引用的 `{item}` 在代码中找不到唯一的文件或行号越界，写相对仓库根的完整路径"
                for item in dict.fromkeys(self.unresolved)]


def parse_location(value: Any) -> tuple[str, int, int] | None:
    if isinstance(value, Mapping) and isinstance(value.get(FILE_KEY), str) and isinstance(value.get(LINE_KEY), int):
        return value[FILE_KEY], value[LINE_KEY], value[LINE_KEY]
    if isinstance(value, str):
        match = WHOLE.fullmatch(value.strip().strip("`"))
        if match:
            return match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))
    return None


def complete(value: Any, snapshot: Snapshot, keys: Iterable[str] | None = None) -> Completed:
    """补全 value 中的位置；keys 给出时只处理映射中的这些键(写 Issue 时不动主张与信号原文)。"""
    snapshot.unresolved = []
    if keys is not None and isinstance(value, Mapping):
        chosen = set(keys)
        done: Any = {key: _complete(snapshot, item) if key in chosen else item for key, item in value.items()}
    else:
        done = _complete(snapshot, value)
    return Completed(done, tuple(snapshot.unresolved))


def check(output: Mapping[str, Any], claim: Mapping[str, Any], snapshot: Snapshot, *, vague_words: Sequence[str],
          light: bool = False) -> list[str]:
    """一次取证输出的全部检查，返回不通过的原因(空表示通过)。light 为轻量推测：不要求反证追到入口。"""
    reasons = verdict_fields(output)
    if reasons:
        return reasons
    reasons += evidence_locations(output, snapshot)
    if output["verdict"] == "refuted":
        reasons += refuted_source(output, claim, snapshot)
    if not light:
        reasons += counter_check(output, claim, snapshot)
    reasons += vague_wording(output, vague_words)
    reasons += assessment_problems(output, snapshot)
    return reasons


def verdict_fields(output: Mapping[str, Any]) -> list[str]:
    verdict = output.get("verdict")
    if verdict not in VERDICTS:
        return [f"[verdict] 判定为 {verdict!r}，只能是 {'、'.join(VERDICTS)}"]
    if verdict == "insufficient" and not output.get("missingInfo"):
        return ["[verdict] 判定为证据不足时必须写明缺少的信息 missingInfo"]
    if verdict == "conditional" and not output.get("trigger"):
        return ["[verdict] 判定为条件成立时必须写明条件 trigger"]
    return []


def evidence_locations(output: Mapping[str, Any], snapshot: Snapshot) -> list[str]:
    found, reasons = 0, []
    for path in EVIDENCE_PATHS:
        for where, value in values_at(output, path):
            if value is None:
                continue
            found += 1
            problem = snapshot.problem(value)
            if problem is not None:
                reasons.append(f"[evidence-location] {where}：{problem}")
    if not found and output.get("verdict") != "insufficient":
        reasons.append("[evidence-location] 没有带「文件路径:行号」的证据")
    return reasons


def refuted_source(output: Mapping[str, Any], claim: Mapping[str, Any], snapshot: Snapshot) -> list[str]:
    source = output.get("sourceOfPhenomenon")
    if not source:
        return ["[refuted-source] 判定为不成立，但没有说明现象从哪里来"]
    location, fact = source.get("location"), source.get("factRef")
    if (location is None) == (fact is None):
        return ["[refuted-source] 现象来源须在 location(挡住它的代码)与 factRef(主张中第几条事实)中恰好给出一个"]
    if fact is not None:
        count = len(claim.get("facts") or [])
        return [] if 1 <= fact <= count else [f"[refuted-source] factRef {fact} 不是主张中事实的序号(共 {count} 条)"]
    problem = snapshot.problem(location)
    return [] if problem is None else [f"[refuted-source] 现象来源的位置不合格：{problem}"]


def counter_check(output: Mapping[str, Any], claim: Mapping[str, Any], snapshot: Snapshot) -> list[str]:
    items = output.get("counterEvidence") or []
    if output.get("verdict") == "insufficient":
        return []
    if not items:
        return ["[counter-check] 没有反证检查，至少沿调用链追到一个入口"]
    entries = set(claim.get("entryPoints") or [])
    reasons = []
    for number, item in enumerate(items, start=1):
        entry = item.get("entry")
        if entry not in entries:
            problem = snapshot.problem(entry)
            if problem is not None:
                reasons.append(f"[counter-check] 第 {number} 条的入口既不是主张中的入口，也不是真实存在的位置：{problem}")
        upstream = item.get("upstreamValidation") or {}
        if upstream.get("status") == "present" and upstream.get("location") is None:
            reasons.append(f"[counter-check] 第 {number} 条写明上游有校验，但没有给出校验的位置")
    return reasons


def vague_wording(output: Mapping[str, Any], words: Sequence[str]) -> list[str]:
    hits = []
    for path in VAGUE_FIELDS:
        for where, value in values_at(output, path):
            if isinstance(value, str):
                hits += [f"{where} 含「{word}」" for word in words if word in value]
    return ["[vague-wording] 结论含含糊措辞：" + "；".join(hits)] if hits else []


def assessment_problems(output: Mapping[str, Any], snapshot: Snapshot) -> list[str]:
    assessment = output.get("assessment")
    if assessment is None:
        if output.get("verdict") in CONFIRMING:
            return ["[assessment] 判为成立或条件成立时必须给出 assessment(价值判断、任务类型、粗规模档与修复方向)"]
        return []
    reasons = [f"[assessment] 预估改动的文件 {item['path']} 在代码中不存在，新建的文件要标明 isNew"
               for item in assessment.get("files") or []
               if not item.get("isNew") and not snapshot.exists(item["path"])]
    if assessment.get("worth") == "defer" and not assessment.get("reevaluateWhen"):
        reasons.append("[assessment] 建议暂不修时必须写明重估条件 reevaluateWhen")
    return reasons


def values_at(value: Any, path: str) -> list[tuple[str, Any]]:
    """按「a.b[*].c」取值，返回(实际路径, 值)；取不到的跳过。"""
    found: list[tuple[str, Any]] = [("", value)]
    for part in path.split("."):
        many = part.endswith("[*]")
        name = part[:-3] if many else part
        step: list[tuple[str, Any]] = []
        for where, node in found:
            if not isinstance(node, Mapping) or name not in node:
                continue
            child, label = node[name], f"{where}.{name}" if where else name
            if many:
                step += [(f"{label}[{index}]", item) for index, item in enumerate(child or [])]
            else:
                step.append((label, child))
        found = step
    return found


def _whole(snapshot: Snapshot, text: str) -> str | None:
    """整个字符串是一个位置时补全后的值；不是位置(文件部分含空白，如「见 a.py:3」是文字)时为 None。"""
    match = WHOLE.fullmatch(text)
    if match is None or re.search(r"\s", match.group(1)) or ("/" not in text and "." not in match.group(1)):
        return None
    resolved = snapshot.resolve(match.group(1))
    return text if resolved is None else resolved + text[match.end(1):]


def _in_text(snapshot: Snapshot, text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        file = match.group(1)
        resolved = snapshot.resolve(file)
        if resolved is None:
            if snapshot.is_code_extension(file):
                snapshot.unresolved.append(match.group(0))
            return match.group(0)
        whole = resolved + match.group(0)[len(file):]
        if snapshot.problem(whole) is not None:
            snapshot.unresolved.append(match.group(0))
        return whole

    return IN_TEXT.sub(replace, text)


def _complete(snapshot: Snapshot, value: Any) -> Any:
    if isinstance(value, Mapping):
        done = {key: _complete(snapshot, item) for key, item in value.items()}
        file = value.get(FILE_KEY)
        if isinstance(file, str) and isinstance(value.get(LINE_KEY), (int, type(None))):
            done[FILE_KEY] = snapshot.resolve(file) or file
        return done
    if isinstance(value, list):
        return [_complete(snapshot, item) for item in value]
    if isinstance(value, str):
        whole = _whole(snapshot, value)
        return whole if whole is not None else _in_text(snapshot, value)
    return value
