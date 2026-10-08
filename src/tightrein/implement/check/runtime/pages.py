"""只对改动涉及的页面：页面巡检与截图(Playwright 用例在工作区 e2e/)。

- 受影响的页面 = 方案交接的 affectedPages，加上路由清单中组件文件属于改动文件的页面路由；路由清单是项目的一个 JSON
  文件(`pages.routes`，相对 worktree：`[{path, componentFile, spec}]`)，没有时只用方案的；
- 只计受影响页面上的失败(用例失败、控制台报错、失败请求；页面路径规范化后比较)，其余页面不影响本次结论；
- 截图只取访问过受影响页面的用例的，或用例文件就是路由清单中该页面的 spec 的(按完整路径段匹配)；
- 巡检本身出错(没装、没有账号、结果不完整)记未验证，不算失败。

页面用例的编写说明(写在工作区 e2e/README，供项目照做；这些都是实际跑出来的教训)：路由从路由清单读，不猜地址(猜错
往往是一张没报错的空页)；优先点导航进入；同名按钮限定所在容器；另外监听未捕获的页面异常；加载跳变按固定间隔采样
中间状态；需要在测试库造真实数据时停下问用户。
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.collect.dedup.normalize import location as normalize_location
from tightrein.implement.check.runtime import page_runner
from tightrein.implement.check.runtime.page_runner import CaseResult, PageRun
from tightrein.implement.check.runtime.verdict import Item, Result, unverified
from tightrein.store.files.layout import WorkspaceLayout

CATEGORY = "pages"
COMMAND = "playwright patrol"
E2E_DIR = "e2e"


@dataclass(frozen=True)
class Screenshot:
    path: str
    page: str


def e2e_dir(workspace: WorkspaceLayout) -> Path:
    """工作区中的页面用例目录(store/files/layout 还没有这一项，先在这里算)。"""
    return workspace.root / E2E_DIR


def routes(worktree: Path, relative: str | None) -> list[Mapping[str, Any]]:
    if not relative or not (worktree / relative).is_file():
        return []
    try:
        data = json.loads((worktree / relative).read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [item for item in data if isinstance(item, Mapping)] if isinstance(data, list) else []


def affected_pages(design: Mapping[str, Any], changed: Collection[str],
                   inventory: Sequence[Mapping[str, Any]]) -> list[str]:
    found = [str(item) for item in design.get("affectedPages") or []]
    files = set(changed)
    found += [str(item["path"]) for item in inventory if item.get("componentFile") in files and item.get("path")]
    return list(dict.fromkeys(found))


def judge(run: PageRun, pages: Sequence[str], inventory: Sequence[Mapping[str, Any]],
          evidence: tuple[str, ...]) -> tuple[Item, list[Screenshot]]:
    if run.status in (page_runner.FAILED, page_runner.SKIPPED):
        return unverified("pages:patrol", CATEGORY, "；".join(run.notes) or "页面巡检没有执行"), []
    wanted = {_normalized(page): page for page in pages}
    failures = [item for item in run.failures if _normalized(item.page) in wanted]
    shots = screenshots(run.cases, wanted, inventory)
    note = "；".join(run.notes) or None
    if failures:
        found = "；".join(f"{item.page} {item.kind} {item.message}" for item in failures)
        return Item("pages:patrol", CATEGORY, Result.FAILED, COMMAND, evidence, f"受影响页面上有失败：{found}"), shots
    if run.status == page_runner.PARTIAL:
        return Item("pages:patrol", CATEGORY, Result.WEAK, COMMAND, evidence, note), shots
    return Item("pages:patrol", CATEGORY, Result.PASSED, COMMAND, evidence, note), shots


def screenshots(cases: Sequence[CaseResult], wanted: Mapping[str, str],
                inventory: Sequence[Mapping[str, Any]]) -> list[Screenshot]:
    """wanted：规范化后的页面路径 → 原写法。"""
    specs = {str(item["spec"]): str(item["path"]) for item in inventory
             if item.get("spec") and _normalized(str(item.get("path") or "")) in wanted}
    found: dict[str, Screenshot] = {}
    for case in cases:
        if case.setup or not case.screenshots:
            continue
        page = _page_of(case, wanted, specs)
        if page is not None:
            for path in case.screenshots:
                found.setdefault(path, Screenshot(path, page))
    return list(found.values())


def _page_of(case: CaseResult, wanted: Mapping[str, str], specs: Mapping[str, str]) -> str | None:
    spec = next((page for file, page in specs.items() if page_runner.same_file(case.file, file)), None)
    if spec is not None:
        return spec
    for observation in case.observations:
        for url in observation.get("pages") or []:
            path = page_runner.page_path(url)
            if path is not None and _normalized(path) in wanted:
                return wanted[_normalized(path)]
    return None


def _normalized(page: str) -> str:
    return normalize_location(page) or page
