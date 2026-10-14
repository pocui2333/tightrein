"""同步到 GitHub Issues：只在新建、放行、关闭、重开时做，少调用 API。

- 是否启用只看接入清单 setup.json 的 `release.github_issues`(enabled 才同步；项目的 origin 还得是 GitHub 仓库)，
  controls."assess.issue".github 只放标签前缀等参数；

- 字段归属：开关状态以 GitHub 为准，其余以本地为准；在 GitHub 上关闭读回为用户关闭(不修)，PR 按 Closes 自动关闭的
  (本地在发布或验收中)只记状态，不当成用户关闭；读回引起的转换不再回写；
- 不留工具痕迹：正文不带标记，缺省不打状态标签，只在新建时按类型打仓库里已有的 bug、enhancement；
- 安全与幂等：公开仓库整次跳过(正文含内部错误与代码位置)；正文去掉历史并整体脱敏；建 Issue 用幂等键，中断后按
  标题找回，不重复建(正文不留本地标记)；失败只记录，下次按本地状态与镜像状态重算差异重试；只移除仓库里确实存在的旧
  标签(GitHub.issue_labels)；
- 正文里反引号中的 `路径:行号` 换成取证 commit 的永久链接；「追加的问题」中各问题的位置是按它自己取证时的 commit
  写的，按它「引用」里的取证 commit 链接(镜像在追加之后才新建时，不会指到 Issue 取证 commit 上的别的行)。
"""

from __future__ import annotations

import re
from functools import partial
from typing import TYPE_CHECKING, Any

from tightrein.onboard.setup import ModuleStatus
from tightrein.protocol.git import GitError, PublicRepository
from tightrein.protocol.git.github import NewIssue

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime
    from tightrein.store.tables.issues import Issue

POINT = "assess.issue"
SETUP_KEY = "release.github_issues"  # 接入清单中的开关
MIRROR = "github"
CODE_LOCATION = re.compile(r"`([^`\s:]+):(\d+)(?:-(\d+))?`")
LOCAL_CLOSED = frozenset({"done", "cancelled"})
PR_CLOSING = frozenset({"releasing", "accepting"})  # PR 合并按 Closes 关闭镜像时本地所在的状态


def enabled(runtime: Runtime) -> bool:
    """接入清单启用了 release.github_issues，且项目的 origin 是 GitHub 仓库。"""
    if runtime.github is None:
        return False
    module = runtime.setup.modules.get(SETUP_KEY)
    return module is not None and module.status is ModuleStatus.ENABLED


def sync(runtime: Runtime, record: Issue) -> None:
    """按本地状态与镜像状态的差异同步(新建、标签、关闭、重开)；失败记进记录，下次重算差异重试。"""
    if not enabled(runtime):
        return
    from tightrein.assess.issue import files

    mirror = dict(record.extra.get(MIRROR) or {})
    if mirror.get("skipped"):
        return
    try:
        mirror = _apply(runtime, record, mirror)
        mirror.pop("error", None)
    except PublicRepository as error:
        mirror = {"skipped": str(error)}
    except GitError as error:
        mirror["error"] = str(error)
    if mirror != record.extra.get(MIRROR):
        record.extra[MIRROR] = mirror
        files.write(runtime, record)


def retry_failed(runtime: Runtime) -> int:
    """上次没同步成功的再试一次；返回处理的个数。"""
    from tightrein.store.tables import issues

    failed = [issue for issue in issues.find(runtime.conn) if (issue.extra.get(MIRROR) or {}).get("error")]
    for issue in failed:
        sync(runtime, issue)
    return len(failed)


def read_back(runtime: Runtime) -> list[str]:
    """读回在 GitHub 上的关闭与重开；返回发生转换的本地 Issue 编号。"""
    if not enabled(runtime):
        return []
    from tightrein.assess.issue.transitions import WONT_FIX, IssueEvent, apply_event
    from tightrein.store.tables import issues

    assert runtime.github is not None
    remote = runtime.github.issue_states()
    changed = []
    for issue in issues.find(runtime.conn):
        mirror = issue.extra.get(MIRROR) or {}
        found = remote.get(int(mirror["number"])) if mirror.get("number") else None
        if found is None or found.state == mirror.get("state"):
            continue
        if found.state == "closed" and issue.status not in LOCAL_CLOSED and issue.status not in PR_CLOSING:
            apply_event(runtime, issue.id, IssueEvent.CANCEL, reason=WONT_FIX, actor="github",
                        note="在 GitHub 上关闭", sync_github=False)
            changed.append(issue.id)
        elif found.state == "open" and issue.status in LOCAL_CLOSED:
            apply_event(runtime, issue.id, IssueEvent.REOPEN, actor="github", note="在 GitHub 上重新打开",
                        sync_github=False)
            changed.append(issue.id)
        _remember_state(runtime, issue.id, found.state)
    return changed


def mirror_body(runtime: Runtime, record: Issue, text: str) -> str:
    """镜像正文：去掉历史一节，代码位置换成永久链接，整体脱敏，末尾带本地标记。"""
    from tightrein.assess.issue import body

    kept = text
    history = body.heading("history", runtime.language)
    marker = f"\n## {history}\n"
    if marker in kept:
        kept = kept[:kept.index(marker)]
    if runtime.github is not None:
        slug = runtime.github.slug
        commit = record.extra.get("triageCommit")
        kept = "\n".join(_linked(slug, part, at) for part, at in body.by_commit(kept, str(commit) if commit else None))
    return runtime.redactor.text(kept.rstrip()) + "\n"


def public_id(record: Issue) -> str:
    """对外的 Issue 编号：有 GitHub 镜像时用 GitHub 编号(人在 GitHub 上看到的)，否则用本地编号。分支名与 PR 标题用它。"""
    return mirror_number(record) or record.id


def mirror_number(record: Issue) -> str | None:
    """GitHub 镜像的编号；没有镜像时为 None。"""
    found: Any = record.extra.get(MIRROR)
    number = found.get("number") if isinstance(found, dict) else found
    return str(number) if isinstance(number, int | str) and str(number).isdigit() else None


def label_of(runtime: Runtime, status: str) -> str | None:
    """状态标签：缺省不打(statusLabels 为假)，人不会随着进度反复改标签，带工具前缀的标签也一眼看得出来。"""
    found = runtime.settings.section(POINT)["github"]
    return f"{found['labelPrefix']}{status}" if found.get("statusLabels") else None


def kind_label(github: Any, record: Issue) -> tuple[str, ...]:
    """新建时按类型打一个仓库里已有的标签(缺陷 bug、需求 enhancement)；仓库没有就不打，不替人新建标签。"""
    wanted = "enhancement" if record.kind == "feature" else "bug"
    return (wanted,) if wanted in github.labels() else ()


def _apply(runtime: Runtime, record: Issue, mirror: dict[str, Any]) -> dict[str, Any]:
    from tightrein.assess.issue import files

    github = runtime.github
    assert github is not None
    scope = runtime.scope(record.id, POINT)
    label = label_of(runtime, record.status)
    if label is not None and label not in github.labels():
        github.label_create(label, "ededed", "", scope=scope)
    if not mirror.get("number"):
        text = mirror_body(runtime, record, files.read_body(runtime.workspace, record.id))
        ref = github.issue_create(NewIssue(title=record.title, body=text, marker="",
                                           labels=(label,) if label else kind_label(github, record)), scope=scope)
        mirror = {"number": ref.number, "url": ref.url, "state": "open", "label": label}
    number = int(mirror["number"])
    if label is not None and mirror.get("label") != label:
        github.issue_labels(number, [label], [mirror["label"]] if mirror.get("label") else [], scope=scope)
        mirror["label"] = label
    wanted = "closed" if record.status in LOCAL_CLOSED else "open"
    if wanted != mirror.get("state"):
        if wanted == "closed":
            reason = "completed" if record.extra.get("closeReason") == "fixed" else "not planned"
            github.issue_close(number, reason, scope=scope)
        else:
            github.issue_reopen(number, scope=scope)
        mirror["state"] = wanted
    return mirror


def _remember_state(runtime: Runtime, issue_id: str, state: str) -> None:
    from tightrein.assess.issue import files
    from tightrein.store.tables import issues

    issue = issues.get(runtime.conn, issue_id)
    if issue is None:
        return
    mirror = dict(issue.extra.get(MIRROR) or {})
    mirror["state"] = state
    issue.extra[MIRROR] = mirror
    files.write(runtime, issue)


def _linked(slug: str, text: str, commit: str | None) -> str:
    """text 中反引号里的 `路径:行号` 换成 commit 的永久链接；不知道 commit 时不换。"""
    return CODE_LOCATION.sub(partial(_link, slug, commit), text) if commit else text


def _link(slug: str, commit: str, match: re.Match[str]) -> str:
    path, start, end = match.group(1), match.group(2), match.group(3)
    lines = f"L{start}" + (f"-L{end}" if end else "")
    return f"[`{path}:{start}{'-' + end if end else ''}`](https://github.com/{slug}/blob/{commit}/{path}#{lines})"
