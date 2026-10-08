"""openapi_file：读现成的接口描述文件(JSON 或 YAML)。

base 为 repo 时 path 相对仓库根，按被测部署的 commit 用 `git show <commit>:<path>` 读(没有部署记录时读
origin/<主分支>)，不需要 worktree，结果按 commit 缓存；为 workspace 时相对工作区根，供仓库里没有接口描述、由工作区
维护一份的项目用，每次重新读。path 不能越出基准目录；文件不存在时报「不适用」(api_fuzz 跳过)。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from tightrein.collect.api_fuzz.spec import SpecNotApplicable, SpecRequest, SpecText
from tightrein.collect.common.source import SourceMisconfigured

REPO = "repo"
WORKSPACE = "workspace"
BASE_NAMES = {REPO: "仓库", WORKSPACE: "工作区"}


def cache_key(request: SpecRequest) -> str | None:
    """只有取自仓库、且知道 commit 的才按 commit 缓存。"""
    return request.commit if request.options["base"] == REPO and request.commit else None


def describe(request: SpecRequest) -> str:
    return f"{BASE_NAMES[request.options['base']]}的 {request.options['path']}"


def fetch(request: SpecRequest) -> SpecText:
    relative = _relative(request.options["path"])
    label = describe(request)
    if request.options["base"] == REPO:
        revision = request.commit or f"origin/{request.git.main_branch}"
        text = request.git.show(revision, relative)
        if text is None:
            raise SpecNotApplicable(f"{revision} 中没有接口描述文件 {relative}")
        return SpecText(text, f"{label}({revision})")
    path = request.workspace / relative
    if not path.is_file():
        raise SpecNotApplicable(f"工作区中没有接口描述文件 {relative}")
    return SpecText(path.read_text(encoding="utf-8"), label)


def _relative(value: str) -> str:
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts or "\\" in value:
        raise SourceMisconfigured(f'controls."collect.api_fuzz".openapi_file.path 越出了基准目录：{value!r}')
    return pure.as_posix()
