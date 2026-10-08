"""发布的流程：提交 → 同步 main → 推送 → PR → 等 CI → 合并 → 跟踪部署 → 验收 → 清理。纯程序，不调用模型。

`release(runtime, issue)` 从 Issue 当前的状态接着推进，能做多少做多少，遇到要等的(CI、部署、观察期)或要人决定的就停：
- 发布中(releasing)：提交、同步、推送、提 PR(都幂等，重来不重复)；合并 main 改到同一文件或解决过冲突时退回实施重新
  审查；提完 PR 当场等 CI 结束并判断合并，不等下一次定时运行。PR 已被人合并或关闭的按实际状态处理；
- 验收中(accepting)：找包含合并提交的部署，按问题来源确认(Issue 正文中只能在部署后确认的验收标准在这里按指纹对应到
  确认结论，写进交接的 criteria)；全部通过转为完成，写交付文档，清理本地；回归时先提
  撤销 PR，再退回待修；接入清单不启用 release.accept 时部署完成即完成，不观察；
- 停下要人处理的(冲突、检查未通过、交付与工作区不符、分支名不合规)写待决定文档，Issue 转为待决定；
- 提交、同步、推送、提 PR 执行前发现前置条件已变(protocol/git 的 Stale：做决定之后仓库或 PR 被别人动过)：不执行旧决定，
  记一条事件，Issue 留在发布中，下次运行重新观察、重新决定(这不是要人处理的事)。
每一步各落盘一份 handoff.json；状态转换都经 assess/issue/transitions。
"""

from __future__ import annotations

import time

from tightrein.assess.issue import body
from tightrein.assess.issue import files as issue_files
from tightrein.assess.issue.transitions import FIX_REJECTED, PROBLEMS, IssueEvent, IssueStatus
from tightrein.implement.implement import StepOutcome
from tightrein.protocol import documents
from tightrein.protocol.git import Git, GitError, GitHub, PullRequest, Stale, resolve
from tightrein.protocol.git.github import MERGED
from tightrein.protocol.handoff import Status
from tightrein.protocol.naming import format_iso, parse_iso
from tightrein.protocol.runtime import Runtime
from tightrein.release import ci, commit, deploy, merge, pr, push, sync
from tightrein.release.accept import confirm, revert
from tightrein.release.cleanup import DONE, cleanup
from tightrein.release.record import (
    POINT_ACCEPT,
    POINT_CI,
    POINT_CLEANUP,
    POINT_DEPLOY,
    POINT_MERGE,
    POINT_PR,
    Delivery,
    DeliveryMissing,
    ReleaseBlocked,
    ReleaseState,
    attach,
    delivery,
    emit,
    load_state,
    move,
    now_iso,
    save_state,
    write_handoff,
)
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

CLOSED = "CLOSED"
REVIEW_STEP = "implement.review"
COMMAND = "tightrein run --object {issue}"
ACCEPT_SKIPPED = "skipped"
NOT_OBSERVED = "接入清单未启用验收(release.accept)：合并并部署即完成，不观察回归"
ACCEPT_UNTIL = "acceptUntil"  # 验收观察期截止(ISO UTC)，status、watch 显示用(44c)


def release(runtime: Runtime, issue_id: str) -> StepOutcome:
    issue = issues.get(runtime.conn, issue_id)
    if issue is None:
        raise LookupError(f"没有 Issue {issue_id}")
    if issue.held_by is not None or issue.status not in (IssueStatus.RELEASING, IssueStatus.ACCEPTING):
        return StepOutcome(issue_id, POINT_PR, Status.PASSED, f"Issue 当前为 {issue.status}，不在发布中", None)
    try:
        if issue.status == IssueStatus.RELEASING:
            return _release(runtime, issue)
        return _accept(runtime, issue)
    except Stale as error:
        return _stale(runtime, issue_id, error)
    except (ReleaseBlocked, DeliveryMissing) as error:
        blocked = error if isinstance(error, ReleaseBlocked) else ReleaseBlocked(POINT_PR, str(error))
        return _hold(runtime, issues.get(runtime.conn, issue_id) or issue, blocked)


# 发布中


def _release(runtime: Runtime, issue: Issue) -> StepOutcome:
    started = time.monotonic()
    github = _github(runtime)
    state = load_state(issue)
    state.entered_at = state.entered_at or now_iso(runtime)
    if issue.pr is not None:
        handled = _closed_elsewhere(runtime, issue, state, github)
        if handled is not None:
            return handled
    found = delivery(runtime, issue.id)
    conventions = resolve(runtime.settings, runtime.git.repo)
    worktree = runtime.git.at(found.worktree)
    committed = commit.commit(runtime, issue, found, conventions, worktree)
    try:
        synced = sync.sync(runtime, issue.id, found, state, worktree)
    except ReleaseBlocked:
        save_state(runtime, issue, state)  # 冲突文件清单要留到用户解决之后
        raise
    if synced.merged is not None:
        state.synced_main = synced.main
    if synced.needs_review:
        return _back_to_review(runtime, issue, state, synced, started)
    pushed = push.push(runtime, issue.id, found.branch, worktree)
    state.pushed = pushed.commit or state.pushed or _remote_head(worktree, found.branch)
    pull = pr.open_pull(runtime, issue, found, conventions, github)
    state.pr_url = pull.url
    pr.post_review(runtime, issue.id, pull.number, found, state, github)
    issue.pr, issue.branch = pull.number, found.branch
    save_state(runtime, issue, state)
    write_handoff(runtime, issue.id, POINT_PR, Status.PASSED, f"PR #{pull.number} 已是最新：{pull.url}", {
        "branch": found.branch, "commit": committed, "syncedMain": synced.main if synced.merged else None,
        "overlap": list(synced.overlap), "pushed": state.pushed, "pushedCommits": list(pushed.commits),
        "pr": pull.number, "url": pull.url, "title": pull.title,
    }, started=started)
    return _ci_and_merge(runtime, issue, found, state, pull.number, github)


def _ci_and_merge(runtime: Runtime, issue: Issue, found: Delivery, state: ReleaseState, number: int,
                  github: GitHub) -> StepOutcome:
    started = time.monotonic()
    native = merge.native(runtime, github)
    required = merge.requires_checks(runtime, github)
    # 交给 GitHub 合并时只看当前的检查结果、开了自动合并就交给它(不在这里等)；由本工具合并时先等 CI 结束再判断
    checks = ci.current(runtime, github, number, required=required) if native else \
        ci.wait(runtime, github, number, required=required)
    decision = merge.decide(runtime, issue.id, number, found, state, checks, github)
    state.ci = checks.to_json()
    save_state(runtime, issue, state)
    write_handoff(runtime, issue.id, POINT_CI, Status.FAILED if checks.state == ci.FAILED else Status.PASSED,
                  f"CI {checks.state}", checks.to_json(), started=started)
    write_handoff(runtime, issue.id, POINT_MERGE, Status.PENDING if decision.gate else Status.PASSED,
                  "已合并" if decision.merged else "已交给 GitHub 自动合并" if decision.native
                  else "未自动合并：" + "；".join(decision.reasons),
                  {"merged": decision.merged, "native": decision.native, "reasons": decision.reasons,
                   "gate": decision.gate, "head": state.pushed}, started=started)
    if decision.merged:
        return _merged(runtime, issue, state, github.pr_view(number))
    if decision.gate is not None:
        return _to_gate(runtime, issue, state, decision, number)
    if checks.state == ci.FAILED:
        raise ReleaseBlocked(POINT_CI, checks.reason or "CI 检查未通过",
                             options=("回到实施修正后重新发布", "确认与本修复无关后在 GitHub 上重新运行检查"))
    summary = "已开启 GitHub 自动合并，等检查通过后由 GitHub 合并" if decision.native \
        else "未自动合并：" + "；".join(decision.reasons)
    return StepOutcome(issue.id, POINT_MERGE, Status.PASSED, summary, POINT_MERGE)


def _closed_elsewhere(runtime: Runtime, issue: Issue, state: ReleaseState, github: GitHub) -> StepOutcome | None:
    """PR 已被人合并(GitHub 自动合并、人工合并)或关闭：按实际状态处理；仍打开时为 None。"""
    pull = github.pr_view(int(issue.pr or 0))
    if pull.state == MERGED:
        return _merged(runtime, issue, state, pull)
    if pull.state != CLOSED:
        return None
    note = _user_note(runtime, github, pull.number)
    attach(issue, state)
    move(runtime, issue, IssueEvent.FAIL, point=POINT_MERGE, reason=FIX_REJECTED,
         note=f"PR #{pull.number} 关闭且未合并" + (f"；用户说明：{note}" if note else ""))
    return StepOutcome(issue.id, POINT_MERGE, Status.PASSED, f"PR #{pull.number} 关闭且未合并，以修复未采纳取消", None)


def _merged(runtime: Runtime, issue: Issue, state: ReleaseState, pull: PullRequest) -> StepOutcome:
    state.merged_at = format_iso(pull.merged_at) if pull.merged_at else now_iso(runtime)
    attach(issue, state)
    move(runtime, issue, IssueEvent.MERGE, point=POINT_MERGE,
         note=f"PR #{pull.number} 已合并，合并提交 {pull.merge_commit}",
         updates={"pr": pull.number, "merge_commit": pull.merge_commit, "gate": None})
    return StepOutcome(issue.id, POINT_MERGE, Status.PASSED, f"PR #{pull.number} 已合并，进入部署跟踪", POINT_DEPLOY)


def _back_to_review(runtime: Runtime, issue: Issue, state: ReleaseState, synced: sync.SyncResult,
                    started: float) -> StepOutcome:
    """合并进来的 main 改到了修复的文件(或解决过冲突)：审查过的改动变了，退回实施重新审查，不接着推送。"""
    resolved = "、".join(f"{path}({side})" for path, side in synced.resolutions.items())
    reason = "合并 origin/main 后" + (f"与修复改到同一文件：{'、'.join(synced.overlap)}" if synced.overlap
                                       else f"解决过冲突：{resolved}")
    write_handoff(runtime, issue.id, POINT_PR, Status.PASSED, reason + "，退回实施重新审查", {
        "syncedMain": synced.main, "merged": synced.merged, "overlap": list(synced.overlap),
        "resolutions": synced.resolutions}, started=started)
    attach(issue, state)
    move(runtime, issue, IssueEvent.START, point=POINT_PR, note=reason, updates={"step": REVIEW_STEP})
    return StepOutcome(issue.id, POINT_PR, Status.PASSED, reason + "，退回实施重新审查", REVIEW_STEP)


def _to_gate(runtime: Runtime, issue: Issue, state: ReleaseState, decision: merge.MergeDecision,
             number: int) -> StepOutcome:
    """高风险路径或人工合并关卡：写待决定文档(同一组原因只写一次)，Issue 留在发布中，等人在 GitHub 上合并。"""
    if decision.new_gate:
        documents.pending(runtime, issue.id, point=POINT_MERGE, decision=f"是否合并 PR #{number}：{state.pr_url}",
                          options=["审阅改动后在 GitHub 上合并", "要求补充审查后再合并", "关闭 PR，不采用这次修复"],
                          recommendation="审阅列出的文件后在 GitHub 上合并",
                          reason=decision.gate_reason or "", if_not="PR 一直保持打开，修复不会上线",
                          command=COMMAND.format(issue=issue.id))
    issue.gate = decision.gate
    save_state(runtime, issue, state)
    return StepOutcome(issue.id, POINT_MERGE, Status.PENDING, decision.gate_reason or "等人决定是否合并", POINT_MERGE)


# 验收中


def _accept(runtime: Runtime, issue: Issue) -> StepOutcome:
    started = time.monotonic()
    state = load_state(issue)
    merge_commit = issue.merge_commit or ""
    merged_at = parse_iso(state.merged_at) if state.merged_at else runtime.clock.now()
    try:
        deployed = deploy.track(runtime, merge_commit, merged_at)
    except (deploy.DeployError, GitError) as error:
        # 只读查询失败只跳过这个 Issue 并写明原因，不中断其他 Issue
        emit(runtime, issue.id, POINT_DEPLOY, "effect", f"读取部署记录失败，下次再试：{error}")
        return StepOutcome(issue.id, POINT_DEPLOY, Status.PASSED, f"读取部署记录失败，下次再试：{error}", POINT_DEPLOY)
    if deployed.status != deploy.SUCCEEDED:
        return _not_deployed(runtime, issue, state, deployed, started)
    if state.deploy is None or state.deploy.get("status") != deploy.SUCCEEDED:
        record = deployed.record
        # 观察期从发现部署成功的时刻算起：平台记的是部署开始的时间，开始到上线之间旧代码报的错不算回归
        state.deploy = {"commit": record.commit if record else merge_commit, "id": record.id if record else None,
                        "url": record.url if record else None, "status": deploy.SUCCEEDED, "source": deployed.source,
                        "at": format_iso(deployed.at) if record is None and deployed.at else now_iso(runtime)}
        issue.deploy = str(state.deploy["commit"])
    found = _delivered_files(runtime, issue.id)
    manual = deploy.manual_paths(runtime.settings, found)
    if manual and not state.manual_noted:
        state.manual_noted = True
        emit(runtime, issue.id, POINT_DEPLOY, "effect", f"改动了需要手动部署的部分：{'、'.join(manual)}，请联系负责人")
    save_state(runtime, issue, state)
    write_handoff(runtime, issue.id, POINT_DEPLOY, Status.PASSED, f"已部署：{state.deploy['commit'][:12]}",
                  {**state.deploy, "manualPaths": manual}, started=started)
    return _confirm(runtime, issue, state)


def _not_deployed(runtime: Runtime, issue: Issue, state: ReleaseState, deployed: deploy.Deployed,
                  started: float) -> StepOutcome:
    record = deployed.record
    previous = (state.deploy or {}).get("status")
    state.deploy = {"commit": record.commit if record else None, "id": record.id if record else None,
                    "url": record.url if record else None, "status": deployed.status, "source": deployed.source,
                    "at": format_iso(deployed.at) if deployed.at else None}
    save_state(runtime, issue, state)
    if deployed.status == deploy.FAILED:
        summary = f"包含合并提交的部署失败：{(record.url or record.id) if record else ''}；是否由本修复引起请人判断"
        if previous != deploy.FAILED:
            documents.pending(runtime, issue.id, point=POINT_DEPLOY, decision=summary,
                              options=["与本修复无关：等下一次部署", "由本修复引起：提撤销 PR"],
                              recommendation="先看部署日志判断原因", reason=summary,
                              if_not="本修复一直没有上线，验收无法开始", command=COMMAND.format(issue=issue.id))
        write_handoff(runtime, issue.id, POINT_DEPLOY, Status.PENDING, summary, dict(state.deploy), started=started)
        return StepOutcome(issue.id, POINT_DEPLOY, Status.PENDING, summary, POINT_DEPLOY)
    if deployed.source == deploy.MERGE_TIME:
        summary = f"没有部署来源，{state.deploy['at']} 之后视为已部署"
    else:
        summary = "部署进行中" if deployed.status == deploy.RUNNING else "还没有包含合并提交的部署"
    write_handoff(runtime, issue.id, POINT_DEPLOY, Status.PASSED, summary, dict(state.deploy), started=started)
    return StepOutcome(issue.id, POINT_DEPLOY, Status.PASSED, summary, POINT_DEPLOY)


def _confirm(runtime: Runtime, issue: Issue, state: ReleaseState) -> StepOutcome:
    started = time.monotonic()
    if not runtime.setup.enabled(POINT_ACCEPT):
        # 接入清单不启用验收：合并并部署即完成，不观察回归
        state.accept = {"result": ACCEPT_SKIPPED}
        save_state(runtime, issue, state)
        write_handoff(runtime, issue.id, POINT_ACCEPT, Status.PASSED, NOT_OBSERVED,
                      {"result": ACCEPT_SKIPPED, "deployCommit": issue.deploy}, started=started)
        return _done(runtime, issue, state, NOT_OBSERVED)
    deployed_at = parse_iso(str((state.deploy or {})["at"]))
    result = confirm.confirm(runtime.conn, list(issue.extra.get(PROBLEMS) or []), deployed_at,
                             runtime.clock.now(), runtime.settings)
    state.accept = result.to_json()
    issue.extra = {**issue.extra, ACCEPT_UNTIL: max((check.due for check in result.checks),
                                                    default=format_iso(deployed_at))}
    save_state(runtime, issue, state)
    status = {confirm.PASSED: Status.PASSED, confirm.WAITING: Status.PASSED, confirm.REGRESSED: Status.FAILED}
    criteria = result.criteria(body.post_deploy_items(issue_files.read_body(runtime.workspace, issue.id)))
    write_handoff(runtime, issue.id, POINT_ACCEPT, status[result.result], result.summary(),
                  {**result.to_json(), "criteria": criteria, "deployCommit": issue.deploy}, started=started)
    if result.result == confirm.WAITING:
        return StepOutcome(issue.id, POINT_ACCEPT, Status.PASSED, result.summary(), POINT_ACCEPT)
    if result.result == confirm.REGRESSED:
        return _regressed(runtime, issue, state, result.summary())
    return _done(runtime, issue, state, result.summary())


def _regressed(runtime: Runtime, issue: Issue, state: ReleaseState, reason: str) -> StepOutcome:
    """先提撤销 PR(同一回归只提一次)，再退回待修。

    退回待修即开下一次修复尝试(assess/issue/attempts)：这一次的修复分支已合并，本地修复目录清理掉(与验收通过时
    一样不删未合并的、不删有改动的)；PR、合并提交与发布进度由状态转换留档后清空，下一次重新提交、重新提 PR。"""
    if state.revert is None:
        reverted = revert.revert(runtime, issue, issue.merge_commit or "", reason,
                                 resolve(runtime.settings, runtime.git.repo), _github(runtime))
        state.revert = {"branch": reverted.branch, "pr": reverted.number, "url": reverted.url}
        save_state(runtime, issue, state)
    if state.cleanup is None:
        try:
            found: Delivery | None = delivery(runtime, issue.id)
        except DeliveryMissing:
            found = None
        state.cleanup = cleanup(runtime, issue.id, found.worktree, found.branch) if found else "没有交付记录，不清理"
        save_state(runtime, issue, state)
    url = str(state.revert["url"])
    documents.failure(runtime, issue.id, point=POINT_ACCEPT, reason=reason, tried=[f"已提撤销 PR：{url}"],
                      advice="决定是否合并撤销 PR；Issue 已退回待修，会按新的情况重新实施",
                      command=COMMAND.format(issue=issue.id))
    move(runtime, issue, IssueEvent.REGRESS, point=POINT_ACCEPT, note=f"{reason}；撤销 PR：{url}")
    return StepOutcome(issue.id, POINT_ACCEPT, Status.FAILED, f"回归：{reason}；已提撤销 PR {url}", None)


def _done(runtime: Runtime, issue: Issue, state: ReleaseState, summary: str) -> StepOutcome:
    issue = move(runtime, issue, IssueEvent.ACCEPT, point=POINT_ACCEPT, note=summary)
    documents.deliver(runtime, issue.id)
    started = time.monotonic()
    try:
        found: Delivery | None = delivery(runtime, issue.id)
    except DeliveryMissing:
        found = None
    outcome = cleanup(runtime, issue.id, found.worktree, found.branch) if found else "没有交付记录，不清理"
    state.cleanup = outcome
    save_state(runtime, issue, state)
    write_handoff(runtime, issue.id, POINT_CLEANUP, Status.PASSED,
                  "已删除本地修复分支与修复目录" if outcome == DONE else outcome, {"cleanup": outcome}, started=started)
    return StepOutcome(issue.id, POINT_ACCEPT, Status.PASSED, f"验收通过：{summary}", None)


# 停下


def _hold(runtime: Runtime, issue: Issue, blocked: ReleaseBlocked) -> StepOutcome:
    """要人处理：写待决定文档，Issue 转为待决定(写明原因)。"""
    command = blocked.command or COMMAND.format(issue=issue.id)
    documents.pending(runtime, issue.id, point=blocked.point, decision=blocked.reason,
                      options=list(blocked.options) or ["处理后重新发布", "取消这个 Issue"],
                      recommendation=(blocked.options or ("处理后重新发布",))[0], reason=blocked.reason,
                      if_not="这个 Issue 停在发布，不会合并", command=command)
    move(runtime, issue, IssueEvent.FAIL, point=blocked.point, reason=blocked.reason)
    return StepOutcome(issue.id, blocked.point, Status.PENDING, blocked.reason, blocked.point)


def _stale(runtime: Runtime, issue_id: str, error: Stale) -> StepOutcome:
    """前置条件已变：不执行旧决定，记事件，留在发布中等下次重新观察。"""
    summary = f"{error}；下次运行重新观察后再决定"
    emit(runtime, issue_id, POINT_PR, "effect", summary)
    return StepOutcome(issue_id, POINT_PR, Status.PASSED, summary, POINT_PR)


# 内部函数


def _github(runtime: Runtime) -> GitHub:
    if runtime.github is None:
        raise ReleaseBlocked(POINT_PR, "项目仓库的 origin 不是 GitHub 仓库(或 gh 不可用)，发布无法提 PR")
    return runtime.github


def _user_note(runtime: Runtime, github: GitHub, number: int) -> str | None:
    """PR 上最后一条评论或评审的正文，作为用户关闭 PR 的说明。"""
    found = github.query_json("pr", "view", str(number), "--json", "comments,reviews") or {}
    bodies = [str(item.get("body") or "").strip() for item in [*(found.get("comments") or []),
                                                               *(found.get("reviews") or [])]]
    bodies = [body for body in bodies if body]
    return bodies[-1] if bodies else None


def _remote_head(worktree: Git, branch: str) -> str | None:
    """之前推送过、这次没有新提交时，最近一次推送的就是 origin/<分支>(等于 HEAD 才算)。"""
    head = worktree.head().commit
    remote = f"origin/{branch}"
    return head if worktree.has_commit(remote) and worktree.rev_parse(remote) == head else None


def _delivered_files(runtime: Runtime, issue: str) -> tuple[str, ...]:
    try:
        return delivery(runtime, issue).changed_files
    except DeliveryMissing:
        return ()

