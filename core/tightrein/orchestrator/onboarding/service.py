"""接入流程(redesign/10-onboarding.md)：工作区的阶段(workspace_meta.phase)、接入清单的检查与回答、onboarding.md。

- 阶段：onboarding(接入中)与 running(运行中)。接入中只做只读的事：识别技术栈、试连接已配置的平台与扩展、在基准版本上
  自检检查命令；不采集、不修代码、不提 PR、不建 Issue(编排只运行接入检查，orchestrator/service.py)。
- 清单按配置动态生成(_evaluate)，状态存 onboarding_items，渲染为工作区根目录的 onboarding.md(progress 类型)，
  每项注明自动完成(done)、需要用户回答(blocked，附推荐答案)、失败待处理(failed)。
- 回答(answer)：采用推荐答案(写回 project.yaml 的对应键，config/edit.py)、跳过(不接入，只记下决定)或给出值；用户也可以
  直接改 project.yaml，或在 onboarding.md 的数据块中把某项改为 done，下次检查采纳。
- 清单全部完成(回答过的问题算完成)、没有失败项时转为运行中。运行中的工作区也可以检查一次(workspace check)，不改阶段。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import tzinfo
from pathlib import Path
from typing import Any

from tightrein.config import edit
from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, format_iso, parse_iso
from tightrein.domain.enums import DocumentStatus, ExtensionPoint
from tightrein.domain.handoff.document import Decision, Event, HandoffDocument, Header, NextStep
from tightrein.orchestrator.onboarding import checklist
from tightrein.orchestrator.onboarding.checklist import BLOCKED, DONE, FAILED, Answer, Item, Outcome
from tightrein.pipeline.common import conventions
from tightrein.store.files import documents
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import onboarding_items, workspace_meta
from tightrein.store.repos.onboarding_items import OnboardingItem

ONBOARDING, RUNNING = "onboarding", "running"
HISTORY_KEY = "onboarding-history"
RECOMMENDED, SKIPPED, VALUE = "recommended", "skip", "value"
MARKED_DONE = "用户在 onboarding.md 中标为完成"
PLATFORMS = (ExtensionPoint.DEPLOY_SOURCE, ExtensionPoint.ERROR_TRACKING, ExtensionPoint.LOG_PLATFORM,
             ExtensionPoint.ALERT_SOURCE)
PLATFORM_NAMES = {ExtensionPoint.DEPLOY_SOURCE: "部署来源", ExtensionPoint.ERROR_TRACKING: "错误追踪",
                  ExtensionPoint.LOG_PLATFORM: "集中日志", ExtensionPoint.ALERT_SOURCE: "业务告警"}
NOT_CONNECTED = {ExtensionPoint.ERROR_TRACKING: "不接入(内部错误只能由 api-fuzz 与静态巡检间接发现)",
                 ExtensionPoint.LOG_PLATFORM: "不接入(不读取应用运行日志与访问日志)",
                 ExtensionPoint.ALERT_SOURCE: "不接入(业务异常可由项目探针补位)"}
SPEC_DRAFT = "openapi.draft.yaml"
SPEC_CONFIRMED = "openapi.yaml"

TryPoint = Callable[[ProjectConfig, ExtensionPoint], Any]  # 返回 PointResult(failure、output)


@dataclass
class OnboardingDeps:
    """try_point 试运行一个扩展点(ext run 的同一路径)；run_checks 把只读 worktree 切到主分支后全量运行检查命令，
    返回失败的命令说明(空表示全部通过)；health 请求被测地址，返回失败原因；account 读取钥匙串条目的账号属性(不读密码)，
    返回失败原因。都为空时对应的项按「未检查」处理。"""

    conn: sqlite3.Connection
    layout: WorkspaceLayout
    clock: Clock
    load_config: Callable[[], ProjectConfig]
    git: Any = None
    try_point: TryPoint | None = None
    run_checks: Callable[[ProjectConfig], list[str]] | None = None
    health: Callable[[ProjectConfig], str | None] | None = None
    account: Callable[[str], str | None] | None = None
    zone: tzinfo | None = None


@dataclass(frozen=True)
class CheckReport:
    summary: str
    items: list[OnboardingItem] = field(default_factory=list)
    phase: str = RUNNING
    remaining: int = 0
    failed: int = 0
    document: Path | None = None


def phase(conn: sqlite3.Connection) -> str:
    return workspace_meta.get(conn, workspace_meta.PHASE) or RUNNING


def set_phase(conn: sqlite3.Connection, clock: Clock, value: str) -> None:
    workspace_meta.set_value(conn, workspace_meta.PHASE, value, clock.now())


class Onboarding:
    def __init__(self, deps: OnboardingDeps) -> None:
        self.deps = deps

    # 阶段与显示

    def active(self) -> bool:
        return phase(self.deps.conn) == ONBOARDING

    def start(self) -> None:
        set_phase(self.deps.conn, self.deps.clock, ONBOARDING)
        self._history("开始接入")

    def counts(self) -> tuple[int, int]:
        found = onboarding_items.all_items(self.deps.conn)
        return (sum(item.state == BLOCKED for item in found), sum(item.state == FAILED for item in found))

    def status_line(self) -> str | None:
        """status 中的一行；运行中时为空。"""
        if not self.active():
            return None
        remaining, failed = self.counts()
        name = self.deps.load_config().name
        return f"{name}：接入中，还差 {remaining} 项需要回答" + (f"，{failed} 项失败待处理" if failed else "")

    def digest_lines(self) -> list[str]:
        """每日汇总「接入中的项目」一节。"""
        line = self.status_line()
        if line is None:
            return []
        return [line, *(f"{item.title}：{item.detail}" + (f"(推荐：{item.recommendation})" if item.recommendation
                                                         else "")
                        for item in onboarding_items.all_items(self.deps.conn) if item.state != DONE)]

    # 检查

    def check(self) -> CheckReport:
        deps = self.deps
        config = deps.load_config()
        self._adopt_file_marks()
        stored = {item.item: item for item in onboarding_items.all_items(deps.conn)}
        now = deps.clock.now()
        saved: list[OnboardingItem] = []
        for position, item in enumerate(self._evaluate(config)):
            outcome = item.outcome
            previous = stored.get(item.id)
            answer = previous.answer if previous is not None else None
            if outcome.state == BLOCKED and answer:
                outcome = Outcome(DONE, f"已回答：{answer}")
            record = OnboardingItem(item.id, position, item.title, outcome.state, outcome.owner, outcome.detail, now,
                                    outcome.recommendation.text if outcome.recommendation else None, answer)
            onboarding_items.save(deps.conn, record)
            saved.append(record)
        onboarding_items.remove_except(deps.conn, {item.item for item in saved})
        remaining = sum(item.state == BLOCKED for item in saved)
        failed = sum(item.state == FAILED for item in saved)
        if self.active() and remaining == 0 and failed == 0:
            set_phase(deps.conn, deps.clock, RUNNING)
            self._history("清单全部完成，转为运行中")
        current = phase(deps.conn)
        summary = ("接入中：" if current == ONBOARDING else "运行中：") + \
            f"完成 {len(saved) - remaining - failed} 项，还差 {remaining} 项需要回答，{failed} 项失败"
        path = self._render(saved, current, summary)
        return CheckReport(summary, saved, current, remaining, failed, path)

    def questions(self) -> list[OnboardingItem]:
        return [item for item in onboarding_items.all_items(self.deps.conn) if item.state == BLOCKED]

    def answer(self, item_id: str, mode: str, value: str | None = None) -> CheckReport:
        """mode：recommended 采用推荐答案，skip 跳过(不接入)，value 采用给出的值。写配置失败时抛出 edit.EditRejected。"""
        deps = self.deps
        config = deps.load_config()
        item = next((found for found in self._evaluate(config) if found.id == item_id), None)
        if item is None:
            raise LookupError(f"接入清单中没有 {item_id}")
        if item.outcome.state != BLOCKED:
            raise ValueError(f"{item_id} 不需要回答：{item.outcome.detail}")
        if mode == RECOMMENDED:
            chosen = item.outcome.recommendation or Answer("跳过")
        elif mode == SKIPPED:
            chosen = Answer("跳过(不接入)")
        else:
            if not value:
                raise ValueError("给出值时 value 不能为空")
            chosen = _with_value(item_id, value) or Answer(value)
        if chosen.key is not None:
            edit.set_value(deps.layout.project_config(), chosen.key, chosen.value)
        record = onboarding_items.get(deps.conn, item_id)
        if record is not None:
            onboarding_items.save(deps.conn, replace(record, answer=chosen.text, updated_at=deps.clock.now()))
        self._history(f"回答 {item_id}：{chosen.text}")
        return self.check()

    # 清单

    def _evaluate(self, config: ProjectConfig) -> list[Item]:
        repo = config.repo
        if not repo.is_dir():
            return [Item("stack", "识别技术栈", Outcome(FAILED, f"仓库不存在：{repo}"))]
        found = checklist.stacks(repo, config.get("onboarding.stackMarkers") or {})
        detail = "、".join(f"{name}({'、'.join(files)})" for name, files in found.items()) or "没有识别到常见技术栈的标记文件"
        items = [Item("stack", "识别技术栈", Outcome(DONE, detail)), self._checks(config, found),
                 self._conventions(config)]
        if config.base_url is not None:
            items += [self._spec(config), self._target(config)]
        items += [self._platform(config, point) for point in PLATFORMS]
        if config.base_url is not None:
            items.append(self._accounts(config))
            roles = self._roles(config)
            if roles is not None:
                items.append(roles)
        return items

    def _checks(self, config: ProjectConfig, found: dict[str, list[str]]) -> Item:
        title = "检查命令在基准版本上通过"
        commands = (config.data.get("checks") or {}).get("commands") or []
        if not commands:
            recommended = checklist.check_commands(found, config.get("onboarding.checkCommands") or {})
            answer = Answer(f"采用 {'、'.join(item['command'] for item in recommended)}", "checks.commands",
                            recommended) if recommended else Answer("暂不配置检查命令(修复无法自检，建议补上)")
            return Item("checks", title, Outcome(BLOCKED, "没有配置检查命令(checks.commands)", answer))
        if self.deps.run_checks is None:
            return Item("checks", title, Outcome(FAILED, "没有提供检查命令的执行器"))
        failures = self.deps.run_checks(config)
        if failures:
            return Item("checks", title, Outcome(FAILED, "主分支上失败：" + "；".join(failures)))
        return Item("checks", title, Outcome(DONE, f"{len(commands)} 条命令在主分支上通过"))

    def _conventions(self, config: ProjectConfig) -> Item:
        title = "项目约定(分支、提交、PR)"
        resolved = conventions.resolve(config, config.repo)
        decided = {key: source for key, source in resolved.sources.items() if source != conventions.GENERIC}
        if "branch" in decided or "commit" in decided:
            return Item("conventions", title, Outcome(DONE, "；".join(f"{key} 来自 {source}"
                                                                      for key, source in decided.items())))
        if self.deps.git is None:
            return Item("conventions", title, Outcome(BLOCKED, "没有写明的约定", Answer("采用通用格式")))
        inferred = conventions.infer_from_repo(self.deps.git, config.repo, config)
        chosen = {key: found.format for key, found in (("branch", inferred.branch), ("commit", inferred.commit))
                  if found.format}
        samples = "；".join(f"{key}：{'、'.join(found.samples)}" for key, found in
                           (("branch", inferred.branch), ("commit", inferred.commit)) if found.samples)
        answer = Answer(f"采用从历史推断的格式 {chosen}", "git.conventions", chosen) if chosen else Answer(
            "采用通用格式(历史中没有统一风格)")
        return Item("conventions", title, Outcome(BLOCKED, f"没有写明的约定；历史样本：{samples or '无'}", answer))

    def _spec(self, config: ProjectConfig) -> Item:
        title = "接口描述"
        if self._configured(config, ExtensionPoint.SPEC_EXPORT):
            return Item("spec", title, self._trial(config, ExtensionPoint.SPEC_EXPORT))
        name = checklist.openapi_file(config.repo)
        if name:
            answer = Answer(f"使用仓库中的 {name}", "extensions.spec-export",
                            {"use": "core/openapi-file", "options": {"path": name}})
            return Item("spec", title, Outcome(BLOCKED, "没有配置接口描述(extensions.spec-export)", answer))
        drafted = (self.deps.layout.root / SPEC_DRAFT).is_file()
        detail = (f"AI 起草的 {SPEC_DRAFT} 待确认：审阅修改后改名为 {SPEC_CONFIRMED}" if drafted else
                  f"框架没有自动导出接口描述：执行 tightrein spec draft 由 AI 起草 {SPEC_DRAFT}，审阅后改名为 {SPEC_CONFIRMED}")
        answer = Answer(f"登记确认后的工作区 {SPEC_CONFIRMED}", "extensions.spec-export",
                        {"use": "core/openapi-file", "options": {"path": SPEC_CONFIRMED, "base": "workspace"}})
        return Item("spec", title, Outcome(BLOCKED, detail, answer))

    def _platform(self, config: ProjectConfig, point: ExtensionPoint) -> Item:
        item_id, title = f"platform:{point.value}", f"平台接入：{PLATFORM_NAMES[point]}"
        if self._configured(config, point):
            return Item(item_id, title, self._trial(config, point))
        answer = checklist.deploy_recommendation(config.repo) if point is ExtensionPoint.DEPLOY_SOURCE else Answer(
            NOT_CONNECTED[point])
        return Item(item_id, title, Outcome(BLOCKED, f"没有配置 extensions.{point.value}", answer))

    def _target(self, config: ProjectConfig) -> Item:
        if self.deps.health is None:
            return Item("target", "被测地址", Outcome(FAILED, "没有提供健康检查"))
        error = self.deps.health(config)
        return Item("target", "被测地址", Outcome(FAILED, f"{config.base_url} 请求失败：{error}") if error else
                    Outcome(DONE, f"{config.base_url} 可以访问"))

    def _accounts(self, config: ProjectConfig) -> Item:
        title = "测试账号"
        roles = config.roles()
        if not roles:
            return Item("accounts", title, Outcome(BLOCKED, "没有配置 accounts", Answer(
                "匿名运行(不配置 accounts)；需要登录的接口按无法判断记录")))
        if self.deps.account is None:
            return Item("accounts", title, Outcome(FAILED, "没有提供钥匙串读取"))
        problems = [f"{role}：{error}" for role in roles if (error := self.deps.account(config.keychain_item(role)))]
        return Item("accounts", title, Outcome(FAILED, "；".join(problems)) if problems else
                    Outcome(DONE, f"{'、'.join(roles)} 的钥匙串条目都存在"))

    def _roles(self, config: ProjectConfig) -> Item | None:
        if not config.roles() or not self._configured(config, ExtensionPoint.AUTHZ_ROLES):
            return None
        return Item("roles", "角色能力表", self._trial(config, ExtensionPoint.AUTHZ_ROLES))

    def _configured(self, config: ProjectConfig, point: ExtensionPoint) -> bool:
        setting = config.extension(point)
        return setting.enabled and (setting.use is not None or setting.command is not None)

    def _trial(self, config: ProjectConfig, point: ExtensionPoint) -> Outcome:
        if self.deps.try_point is None:
            return Outcome(FAILED, "没有提供扩展的试运行")
        try:
            result = self.deps.try_point(config, point)
        except (ValueError, OSError) as error:
            return Outcome(FAILED, f"试运行失败：{error}")
        if result.failure is not None:
            return Outcome(FAILED, f"试运行失败：{result.failure.describe()}")
        return Outcome(DONE, "试运行成功")

    # 文件与历史

    def _history(self, text: str) -> None:
        deps = self.deps
        found = json.loads(workspace_meta.get(deps.conn, HISTORY_KEY) or "[]")
        found.append({"at": format_iso(deps.clock.now()), "text": text})
        workspace_meta.set_value(deps.conn, HISTORY_KEY, json.dumps(found, ensure_ascii=False), deps.clock.now())

    def _adopt_file_marks(self) -> None:
        """onboarding.md 的数据块中被用户改为 done、而数据库中仍需回答的项，记为已回答。"""
        path = self.deps.layout.onboarding_document()
        if not path.is_file():
            return
        try:
            marked = documents.read(path).blocks.get("checklist") or []
        except documents.DocumentError:
            return
        for entry in marked:
            item_id = str(entry.get("item", "")).partition("]")[0].lstrip("[")
            record = onboarding_items.get(self.deps.conn, item_id)
            if entry.get("state") == DONE and record is not None and record.state == BLOCKED and not record.answer:
                onboarding_items.save(self.deps.conn, replace(record, answer=MARKED_DONE,
                                                              updated_at=self.deps.clock.now()))

    def _render(self, saved: Sequence[OnboardingItem], current: str, summary: str) -> Path:
        deps = self.deps
        config = deps.load_config()
        now = deps.clock.now()
        labels = {DONE: "自动完成", BLOCKED: "需要用户回答", FAILED: "失败待处理"}
        lines = [f"[{'x' if item.state == DONE else ' '}] [{item.item}] {item.title}：{item.detail}"
                 f"({labels[item.state] if not item.answer else '已回答'})"
                 + (f"；推荐：{item.recommendation}" if item.state == BLOCKED and item.recommendation else "")
                 for item in saved]
        history = json.loads(workspace_meta.get(deps.conn, HISTORY_KEY) or "[]") or [
            {"at": format_iso(now), "text": "检查接入清单"}]
        blocked = [item for item in saved if item.state == BLOCKED]
        status = DocumentStatus.DONE if current == RUNNING else (
            DocumentStatus.BLOCKED if blocked or any(item.state == FAILED for item in saved)
            else DocumentStatus.IN_PROGRESS)
        document = HandoffDocument(
            Header("progress", "onboarding", status, "loop/onboarding", "user", config.name,
                   parse_iso(history[0]["at"]), now),
            summary + "。",
            {"checklist": "\n".join(f"- {line}" for line in lines),
             "completed": "\n".join(f"- {item.title}" for item in saved if item.state == DONE) or "无",
             "blockers": "\n".join(f"- {item.title}：{item.detail}" for item in saved if item.state == FAILED) or "无"},
            {"checklist": [{"item": f"[{item.item}] {item.title}", "state": item.state, "owner": item.owner}
                           for item in saved]},
            decisions=tuple(Decision(item.title, item.recommendation or "按说明补充配置",
                                     "回车采用推荐(tightrein workspace init)，或执行下一步中的命令") for item in blocked),
            next_steps=tuple(NextStep(f"tightrein workspace answer {item.item} --recommended", "user")
                             for item in blocked),
            history=tuple(Event(parse_iso(entry["at"]), entry["text"]) for entry in history))
        path = deps.layout.onboarding_document()
        documents.write(path, document, config.language, deps.zone)
        return path


def _with_value(item_id: str, value: str) -> Answer | None:
    """用户给出值时写回的配置：检查命令、接口描述文件、部署工作流、分支格式；其余项只记下回答。"""
    if item_id == "checks":
        return Answer(f"检查命令 {value}", "checks.commands", [{"name": "check", "cwd": ".", "command": value}])
    if item_id == "spec":
        return Answer(f"接口描述 {value}", "extensions.spec-export", {"use": "core/openapi-file",
                                                                     "options": {"path": value}})
    if item_id == f"platform:{ExtensionPoint.DEPLOY_SOURCE.value}":
        return Answer(f"部署工作流 {value}", "extensions.deploy-source",
                      {"use": "core/github-actions", "options": {"workflow": value}})
    if item_id == "conventions":
        return Answer(f"分支格式 {value}", "git.conventions", {"branch": value})
    return None

