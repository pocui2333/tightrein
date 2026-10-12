"""已取证问题的证据包：采集时的取证结论，加程序按行号截取的当前代码原文，交给 assess.confirm。

- 只放结论与原文锚点(代码事实、反证、影响、根因)，不放分析过程(analysis)：长篇推理既占 token，又容易被当成导出
  推理而拒答；
- 当前代码由程序从只读 worktree(取证 commit)截取原文：证据里的每个位置前后各 CONTEXT 行，每段最多 MAX_LINES 行，
  同一文件相邻的段合并；总长超过 MAX_CHARS 时截断并写明，需要时按行号自己读；
- 片段一律是原文，不改写(改写过的锚点会让后面的修复对不上行)；位置解析不了或文件已不存在的跳过并写明。
发现问题的版本与取证版本之间改过这些文件的提交，另在主张的「主干差异」里。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from tightrein.assess.notes import LOCATION

CONTEXT = 3
MAX_LINES = 60
MAX_CHARS = 20000
TRUNCATED = "...(已截断：其余位置按上面的 `文件:行号` 自己读)"


def locations(value: Mapping[str, Any]) -> Iterator[str]:
    """证据中的全部位置，按出现顺序：代码事实、根因、反证中的上游校验、影响的调用点。"""
    for fact in value.get("facts") or []:
        yield str(fact.get("location") or "")
    for cause in value.get("rootCauses") or []:
        yield f"{cause.get('file')}:{cause.get('line')}"
    for item in value.get("counterEvidence") or []:
        yield str((item.get("upstreamValidation") or {}).get("location") or "")
    yield from (str(site) for site in (value.get("impact") or {}).get("callSites") or [])


def render(value: Mapping[str, Any], root: Path, commit: str | None) -> str:
    return "\n".join([*_conclusion(value, commit), "", "### 当前代码(程序按行号截取的原文)", "",
                      _code(value, root)])


def _conclusion(value: Mapping[str, Any], commit: str | None) -> list[str]:
    lines = [f"### 采集时的取证(版本 {commit or '未知'})", "", f"- 判定：{value.get('verdict')}"]
    if value.get("trigger"):
        lines.append(f"- 触发条件：{value['trigger']}")
    lines += ["", "#### 代码事实", ""]
    lines += [f"{number}. `{fact.get('location')}`：{fact.get('observation')}"
              for number, fact in enumerate(value.get("facts") or [], start=1)]
    lines += ["", "#### 反证检查", ""]
    for item in value.get("counterEvidence") or []:
        upstream = item.get("upstreamValidation") or {}
        guard = f"有，`{upstream.get('location')}`" if upstream.get("status") == "present" else "无"
        lines.append(f"- {item.get('check')}；入口 `{item.get('entry')}`；上游校验：{guard}；结论：{item.get('result')}")
    impact = value.get("impact")
    if impact:
        sites = "、".join(f"`{site}`" for site in impact.get("callSites") or []) or "无"
        lines += ["", "#### 影响", "", f"- 类别：{impact.get('kind')}", f"- 调用点：{sites}",
                  f"- 后果：{impact.get('consequence')}"]
    causes = value.get("rootCauses") or []
    if causes:
        lines += ["", "#### 根因", ""]
        lines += [f"- `{cause.get('file')}:{cause.get('line')}`" + (f" {cause['symbol']}" if cause.get("symbol") else "")
                  for cause in causes]
    return lines


def _code(value: Mapping[str, Any], root: Path) -> str:
    ranges: dict[str, list[tuple[int, int]]] = {}
    skipped: list[str] = []
    for location in dict.fromkeys(item.strip().strip("`") for item in locations(value) if item.strip()):
        found = LOCATION.match(location)
        if found is None:
            skipped.append(location)
            continue
        start = int(found["start"])
        ranges.setdefault(found["path"], []).append((start, max(int(found["end"] or start), start)))
    parts: list[str] = []
    used = 0
    for path, spans in ranges.items():
        lines = _read(root, path)
        if not lines:
            skipped.append(path)
            continue
        block = "\n  ...\n".join(_numbered(lines, low, high) for low, high in _merge(spans, len(lines)))
        text = f"#### {path}\n\n```\n{block}\n```"
        if used + len(text) > MAX_CHARS:
            parts.append(TRUNCATED)
            break
        parts.append(text)
        used += len(text)
    if skipped:
        parts.append("没能截取(位置解析不了或文件已不存在)：" + "、".join(f"`{item}`" for item in skipped))
    return "\n\n".join(parts) or "(无)"


def _merge(spans: list[tuple[int, int]], length: int) -> list[tuple[int, int]]:
    """每段前后加 CONTEXT 行、最多 MAX_LINES 行，按行号合并相邻或重叠的段。"""
    widened = []
    for start, end in spans:
        if start > length:
            continue
        low = max(1, start - CONTEXT)
        widened.append((low, min(length, end + CONTEXT, low + MAX_LINES - 1)))
    merged: list[tuple[int, int]] = []
    for low, high in sorted(widened):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def _numbered(lines: list[str], low: int, high: int) -> str:
    return "\n".join(f"{number:>5}  {lines[number - 1]}" for number in range(low, high + 1))


def _read(root: Path, path: str) -> list[str]:
    target = (root / path).resolve()
    if root.resolve() not in target.parents or not target.is_file():
        return []
    return target.read_text(encoding="utf-8", errors="replace").splitlines()
