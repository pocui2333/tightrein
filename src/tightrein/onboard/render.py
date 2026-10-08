"""setup.json → setup.md(给人看的接入清单)，不手写；setup.json 一改、每次试跑后都重新生成。

最上面一段汇总：启用、不启用、自定义、待填各几项；单独列出「不启用且没有兜底」的项，提醒存在盲区(只是开关的
release.accept、release.github_issues 不启用的影响是确定的，直接写在「不启用的影响」一列，不算盲区)；
项目事实(settings.json)中探测不到的也列为待填。下面按「阶段 → 模块」逐行列出：位置、状态、用什么、原因、
不启用的影响、试跑结果。读的是原始 JSON：清单不合格时也要能渲染出来，把问题标给人看。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.onboard.check import Trial
from tightrein.onboard.setup import MODULES, SWITCHES, issues_of
from tightrein.store.files.json import read_json
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.markdown import write_markdown

STATUSES = ("enabled", "disabled", "custom")
LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "title": "接入清单：{project}", "generated": "由 setup.json 生成，不要手改；改 setup.json 后执行 tightrein project check。",
        "summary": "汇总", "enabled": "启用", "disabled": "不启用", "custom": "自定义", "unset": "待填",
        "count": "{label} {count} 项", "blind": "盲区(不启用且没有兜底)", "pending": "待填",
        "issues": "清单的问题", "stage": "阶段", "module": "位置", "status": "状态", "using": "用什么",
        "reason": "原因", "impact": "不启用的影响", "trial": "试跑", "none": "无", "fact": "settings.json 的 project.{name}",
        "passed": "通过", "failed": "不通过", "skipped": "跳过", "unchecked": "未试跑", "checked": "上次试跑：{at}",
        "collect": "采集", "implement": "实施", "release": "发布", "hint": "探测依据",
        "baseline": "基线检查(只读 worktree，主分支)", "separator": "，",
        "conventions": "从提交历史推断的约定(确认后再写进 settings.json)",
        "off:release.accept": "合并并部署即完成，不观察回归", "off:release.github_issues": "Issue 只记在本地，不建 GitHub 镜像",
    },
    "en": {
        "title": "Setup: {project}", "generated": "Generated from setup.json; do not edit. Run tightrein project check after changing setup.json.",
        "summary": "Summary", "enabled": "enabled", "disabled": "disabled", "custom": "custom", "unset": "to fill",
        "count": "{label} {count}", "blind": "Blind spots (disabled with no fallback)", "pending": "To fill",
        "issues": "Problems in the checklist", "stage": "Stage", "module": "Module", "status": "Status",
        "using": "Using", "reason": "Reason", "impact": "Impact if disabled", "trial": "Trial", "none": "none",
        "fact": "project.{name} in settings.json", "passed": "passed", "failed": "failed", "skipped": "skipped",
        "unchecked": "not run", "checked": "Last trial: {at}", "collect": "Collect", "implement": "Implement",
        "release": "Release", "hint": "Detected",
        "baseline": "Baseline checks (read-only worktree, main branch)", "separator": ", ",
        "conventions": "Conventions inferred from history (confirm before writing them into settings.json)",
        "off:release.accept": "Done once merged and deployed; regressions are not watched",
        "off:release.github_issues": "Issues stay local; no GitHub mirror",
    },
}


@dataclass(frozen=True)
class Page:
    project: str
    setup: Mapping[str, Any]  # setup.json 原样
    facts: Mapping[str, Any]  # settings.json 的 project 部分原样
    issues: Sequence[str]  # setup.load 报出的问题
    trials: Sequence[Trial]  # 上次试跑的结果；没跑过为空
    checked_at: str | None
    hints: Mapping[str, str]  # 探测依据：模块键 → 一句话
    conventions: Sequence[str] = ()  # 从提交历史推断的约定(接入时给用户确认)


def render(page: Page, language: str) -> str:
    words = LABELS.get(language, LABELS["en"])
    modules: Mapping[str, Any] = page.setup.get("modules") or {}
    entries: dict[str, Mapping[str, Any]] = {
        key: value if isinstance(value := modules.get(key), Mapping) else {} for key in MODULES}
    counts = {status: sum(entry.get("status") == status for entry in entries.values()) for status in STATUSES}
    unset = [key for key, entry in entries.items() if entry.get("status") not in STATUSES]
    blind = [key for key, entry in entries.items()
             if entry.get("status") == "disabled" and not entry.get("impact") and key not in SWITCHES]
    facts = [name for name, value in page.facts.items() if value is None]
    commands = page.facts.get("commands") or {}
    facts += [f"commands.{name}" for name, value in commands.items() if value is None] if isinstance(commands, dict) else []
    lines = [f"# {words['title'].format(project=page.project)}", "", words["generated"], "", f"## {words['summary']}", ""]
    lines.append(words["separator"].join([*(words["count"].format(label=words[status], count=counts[status])
                                             for status in STATUSES),
                                           words["count"].format(label=words["unset"], count=len(unset))]))
    if page.checked_at:
        lines += ["", words["checked"].format(at=page.checked_at)]
    lines += ["", f"### {words['blind']}", ""] + (_bullets(blind) or [words["none"]])
    lines += ["", f"### {words['pending']}", ""] + (
        _bullets(unset + [words["fact"].format(name=name) for name in facts]) or [words["none"]])
    if page.issues:
        lines += ["", f"### {words['issues']}", ""] + _bullets(page.issues)
    if page.conventions:
        lines += ["", f"### {words['conventions']}", ""] + _bullets(page.conventions)
    trials = {trial.key: trial for trial in page.trials}
    header = [words[name] for name in ("module", "status", "using", "reason", "impact", "trial")]
    for stage in dict.fromkeys(key.split(".")[0] for key in MODULES):
        lines += ["", f"## {words.get(stage, stage)}", "", _row(header), _row(["---"] * len(header))]
        for key in (key for key in MODULES if key.startswith(stage + ".")):
            lines.append(_row(_cells(key, entries[key], trials.get(key), page.hints.get(key), words)))
    baseline = [trial for trial in page.trials if trial.key.startswith("baseline")]
    if baseline:
        lines += ["", f"## {words['baseline']}", ""]
        lines += _bullets([f"`{trial.key}`：{words[trial.status]}" + (f"：{trial.detail}" if trial.detail else "")
                           for trial in baseline])
    return "\n".join(lines) + "\n"


def write(path: Path, page: Page, language: str) -> Path:
    write_markdown(path, render(page, language))
    return path


def refresh(workspace: WorkspaceLayout, *, trials: Sequence[Trial], checked_at: str | None, language: str) -> Path:
    """按当前的 setup.json 与 settings.json 重新生成 setup.md(试跑后、`project show` 时)。"""
    data = read_json(workspace.setup) if workspace.setup.is_file() else {}
    facts = read_json(workspace.settings).get("project") or {} if workspace.settings.is_file() else {}
    page = Page(project=workspace.project, setup=data, facts=facts, issues=issues_of(data, workspace), trials=trials,
                checked_at=checked_at, hints={})
    return write(workspace.setup_md, page, language)


def _cells(key: str, entry: Mapping[str, Any], trial: Trial | None, hint: str | None,
           words: Mapping[str, str]) -> list[str]:
    status = entry.get("status")
    using = entry.get("method") or entry.get("script") or ""
    if entry.get("guide"):
        using = f"{using}({entry['guide']})" if using else str(entry["guide"])
    if hint and not using:
        using = f"{words['hint']}：{hint}"
    result = words["unchecked"] if trial is None else f"{words[trial.status]}" + (f"：{trial.detail}" if trial.detail else "")
    impact = entry.get("impact") or (words[f"off:{key}"] if key in SWITCHES and status == "disabled" else "")
    return [f"`{key}`", words.get(str(status), words["unset"]) if status in STATUSES else f"**{words['unset']}**",
            using, entry.get("reason") or "", impact, result]


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |"


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"- {item}" for item in items]
