"""watch 的界面：不用外框，细分隔线分区，总高固定，刷新时不滚屏、不抖动。

自上而下：顶栏(工作区与运行状态、时间)；运行总览两行(阶段、运行与进程号、模型；总耗时、今日 token 用量、状态)；最新一份交接文档(路径与时间、结论两行)；
流程脉络(连续跳过的折叠为「跳过(N)」，当前步之后第一个等待的单列、其余折叠为「等待(N)」)；步骤明细只展开上一步、当前步与
下一步；continue 推进的运行(只有状态恢复一步)改为显示进行中的 Issue 的修复步骤、第几次调用与上一次失败的原因；最近 LOG_LINES 条事件(带表头)；底栏(按键与建议的命令)。数据列定宽，超长的文字在末尾以省略号截断，一行不折行。
状态只用文字加颜色表示，不用符号；配色在浅色与深色背景上都清楚(见 STYLES)。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta, tzinfo

from rich.console import Group, RenderableType
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from tightrein.domain.enums import RunStatus
from tightrein.monitor.snapshot import ActiveIssue, AgentCall, FixStep, Snapshot, StepState

LOG_LINES = 3
HEIGHT = 26  # 界面总行数：顶栏、分隔、总览 2、空行、交接文档 2、分隔、脉络、空行、标题、明细 7、空行、标题、
# 日志表头与 3 条、分隔、底栏
DETAIL_LINES = 7  # 上一步两行、当前步两行、下一步一行，步骤之间各空一行；不足时补空行，保持总高不变
# 按信息的类别配色，同一类在任何位置颜色相同；全部不加粗，层次只靠颜色。参考 Claude Code 与 lazygit：一组饱和度低、
# 亮度相近的柔和色(在浅色与深色背景上都清楚)，鼠尾草绿为主色；终端不支持真彩色时由 rich 换成最接近的 256 色。
# 值只分两类：文字型(标识、模型、路径)与数值。空字符串为终端默认前景色；不用 dim 与浅色。
VALUE, NUMBER = "#66866e", "#a67052"
STYLES = {
    "title": "",                      # 区块标题：步骤明细、最新日志
    "label": "",                      # 标签与表头：阶段:、耗时:、时间
    "decor": "#626262",               # 装饰：分隔线、连接线、>>>、序号、[上一步]、按键提示
    "ident": VALUE, "model": VALUE, "path": VALUE,  # 文字型的值：工作区、运行编号、步骤名、角色、模型、路径
    "number": NUMBER,                 # 数值：耗时、token 用量、时间、进程号
    "text": "",                       # 自由文字：备注、结论、说明
    "done": "#59855b", "running": "#aa823d", "failed": "#a14d48", "interrupted": "#a14d48", "attention": "#a14d48",
    "waiting": "", "skipped": "",
}
STATES = {"done": "已完成", "running": "运行中", "failed": "失败", "interrupted": "中断", "waiting": "等待中",
          "skipped": "跳过"}
NAMES = {"recovery": "状态恢复", "deployments": "部署环境", "on-deploy": "部署探针", "commit-patrol": "代码巡检",
         "sources": "平台探针", "scheduled": "定时探针", "chain-collect": "链路采集", "chain-aggregate": "链路聚合",
         "triage": "缺陷分诊", "issue": "自动提单", "unattended": "无人修复", "release-track": "发布跟踪",
         "verify-staging": "预发验证", "issue-mirror": "镜像同步", "learn-lessons": "经验沉淀", "weekly": "周报汇总",
         "retention": "数据保留", "health": "健康检查"}
RESULTS = {"ok": ("完成", "done"), "failed": ("失败", "failed"), "error": ("失败", "failed"),
           "timeout": ("超时", "failed"), "limit-reached": ("到达上限", "failed"), "schema-invalid": ("格式不符", "failed"),
           "guard-violation": ("越界", "failed"), "create-issue": ("建 Issue", "running"),
           "needs-decision": ("待决定", "failed"), "auto-approve": ("自动放行", "done"),
           "auto-confirm": ("自动确认", "done"), "pass": ("通过", "done"), "deferred": ("暂缓", "waiting"),
           "observe": ("观察", "waiting"), "drop": ("不修", "waiting"), "takeover": ("接管", "waiting")}
# 定宽列(终端字符宽度，中文占两格)
NAME_WIDTH, STATUS_WIDTH, DURATION_WIDTH, USAGE_WIDTH = 30, 16, 16, 14
SUMMARY_WIDTHS = (30, 42)
LOG_WIDTHS = (10, 16, 26, 10, 9, 8)  # 时间、角色、工具与模型、结果、耗时、用量；其后的说明占剩余宽度
LOG_HEADERS = ("时间", "角色", "工具与模型", "结果", "耗时", "用量", "说明")
COLUMN_GAP = 1  # _grid 中每列右侧的空隙
STEP_PREFIX = "  {number}. "
INDENT = len(STEP_PREFIX.format(number=1)) - COLUMN_GAP  # 第二行「模型」与第一行步骤名的第一个字对齐
# 第二行的「模型」占到第一行「耗时」之前，使「备注」与「耗时」上下对齐(每列右侧各有一格空隙)
MODEL_WIDTH = NAME_WIDTH + STATUS_WIDTH - INDENT
THOUSAND, MILLION = 1_000, 1_000_000
SHORT_LIMIT = 100  # 缩写后小于它时保留一位小数(12.3k)，否则取整(758k)
TIME_WIDTH = 18


def render(snapshot: Snapshot, width: int, zone: tzinfo | None, interval: float) -> RenderableType:
    current = _current(snapshot.steps)
    gap = Text("")
    if _continuing(snapshot):
        flow, details = _fix_streamline(_fix_order(snapshot)[0], snapshot), _fix_details(snapshot)
    else:
        flow, details = _streamline(snapshot.steps, current), _details(snapshot, current)
    return Group(_header(snapshot, zone), _divider(), _summary(snapshot, current), gap, _handoff(snapshot, zone),
                 _divider(), flow, gap, Text("步骤明细:", style=STYLES["title"]),
                 *details, gap, Text(f"最新日志 ({LOG_LINES} 条):", style=STYLES["title"]),
                 *_logs(snapshot, zone), _divider(), _footer(snapshot, interval))


def _continuing(snapshot: Snapshot) -> bool:
    """continue 推进的运行只有状态恢复一步，没有定时运行的其他步骤：改为显示进行中的 Issue 的修复进度。"""
    return bool(snapshot.issues) and all(step.name == "recovery" for index, step in enumerate(snapshot.steps)
                                         if index in _executed(snapshot.steps))


def duration(value: timedelta | float | None) -> str:
    """0.4s、48s、1分40秒、2时05分。"""
    if value is None:
        return "-"
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    if seconds < 1:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}分{rest:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}时{minutes:02d}分"


def tokens(count: int) -> str:
    """token 数：999、12.3k、758k、12.3M。"""
    if count < THOUSAND:
        return str(count)
    for size, unit in ((MILLION, "M"), (THOUSAND, "k")):
        if count >= size:
            value = count / size
            return f"{value:.1f}{unit}" if value < SHORT_LIMIT else f"{value:.0f}{unit}"
    return str(count)


def step_name(name: str) -> str:
    return f"{NAMES.get(name, name)} ({name})"


def _divider() -> Rule:
    return Rule(style=STYLES["decor"])


def _grid(*widths: int | None) -> Table:
    """定宽列(None 为占用剩余宽度的伸缩列)；每列不折行，超长时以省略号截断。"""
    grid = Table.grid(expand=True, padding=(0, COLUMN_GAP, 0, 0))  # 定宽内容占满时也不与下一列相连
    for width in widths:
        grid.add_column(width=width, min_width=width, max_width=width, no_wrap=True, overflow="ellipsis",
                        ratio=None if width else 1)
    return grid


def _pair(label: str, value: str | Text, kind: str = "text") -> Text:
    """「标签: 值」；值为字符串时按 kind(STYLES 的类别)着色。"""
    text = Text(f"{label}: ", style=STYLES["label"])
    return text.append_text(value) if isinstance(value, Text) else text.append(value, style=STYLES[kind])


def _state(state: str) -> Text:
    return Text(STATES.get(state, state), style=STYLES.get(state, ""))


def _current(steps: Sequence[StepState]) -> int | None:
    """当前步：运行中、失败或中断的那一步；都没有时为空。"""
    return next((index for index, step in enumerate(steps) if step.state in ("running", "failed", "interrupted")),
                None)


def _executed(steps: Sequence[StepState]) -> list[int]:
    return [index for index, step in enumerate(steps) if step.state in ("done", "running", "failed", "interrupted")]


def _header(snapshot: Snapshot, zone: tzinfo | None) -> Table:
    if snapshot.paused:
        state = Text("已暂停", style=STYLES["interrupted"])
    elif snapshot.running:
        state = Text("运行中 ...", style=STYLES["running"])
    else:
        state = Text("就绪 (空闲)", style=STYLES["done"])
    left = Text("tightrein ", style=STYLES["title"]).append(">>> ", style=STYLES["decor"])
    left.append(f"[{snapshot.workspace}]", style=STYLES["ident"])
    left.append(" ").append_text(state)
    grid = _grid(None, TIME_WIDTH)
    grid.columns[1].justify = "right"
    grid.add_row(left, Text(snapshot.now.astimezone(zone).strftime("%Y-%m-%d %H:%M"), style=STYLES["number"]))
    return grid


def _model_text(model: str | None) -> Text:
    return Text(model or "本地程序", style=STYLES["model"])


def _call_model(agent: AgentCall) -> str:
    text = " ".join(part for part in (agent.tool, agent.model or "") if part)
    return f"{text} ({agent.effort})" if agent.effort else text


def _summary(snapshot: Snapshot, current: int | None) -> Table:
    loop = snapshot.loop
    executed = _executed(snapshot.steps)
    anchor = current if current is not None else (executed[-1] if executed else None)
    focus = snapshot.steps[anchor] if anchor is not None else None
    if snapshot.agents:
        model = _model_text(_call_model(snapshot.agents[-1]))
    elif focus is not None:
        model = _model_text(focus.model)
    else:
        model = Text("-")
    if loop is None:
        run, spent, status = Text("-"), "-", Text("还没有运行过", style=STYLES["waiting"])
    else:
        run = Text(loop.id, style=STYLES["ident"])
        if snapshot.pid:
            run.append(" (PID ").append(str(snapshot.pid), style=STYLES["number"]).append(")")
        end = loop.ended_at or (snapshot.now if snapshot.running else None)
        if end is None and focus is not None and focus.started_at is not None and focus.duration is not None:
            end = focus.started_at + focus.duration
        spent = "-" if end is None else duration(end - loop.started_at)
        status = {RunStatus.RUNNING: _state("running"), RunStatus.INTERRUPTED: _state("interrupted"),
                  RunStatus.FAILED: _state("failed")}.get(loop.status, Text("全部完成", style=STYLES["done"]))
    grid = _grid(*SUMMARY_WIDTHS, None)
    grid.add_row(_pair("阶段", step_name(focus.name) if focus else "-", "ident"), _pair("运行", run),
                 _pair("模型", model))
    grid.add_row(_pair("总耗时", spent, "number"), _pair("今日用量", f"{tokens(snapshot.day_tokens)} tokens", "number"),
                 _pair("状态", status))
    return grid


def _handoff(snapshot: Snapshot, zone: tzinfo | None) -> Group:
    """两行：交接文档的路径与时间；结论。超长时在末尾以省略号截断。"""
    if not snapshot.documents:
        return Group(_single(_pair("交接文档", "暂无交接文档")), _single(_pair("结论", "-")))
    latest = snapshot.documents[0]
    place = _pair("交接文档", latest.path, "path").append("   ")
    place.append_text(_pair("时间", latest.modified_at.astimezone(zone).strftime("%H:%M"), "number"))
    return Group(_single(place), _single(_pair("结论", latest.conclusion)))


def _single(line: Text) -> Text:
    line.no_wrap, line.overflow = True, "ellipsis"
    return line


def _streamline(steps: Sequence[StepState], current: int | None) -> Text:
    """连续跳过的折叠；当前步(没有时为最后执行的一步)之后第一个等待的单列，其余等待的折叠。"""
    executed = _executed(steps)
    anchor = current if current is not None else (executed[-1] if executed else -1)
    parts: list[Text] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        if step.state == "skipped":
            end = index
            while end < len(steps) and steps[end].state == "skipped":
                end += 1
            count = end - index
            label = f"跳过({count})"
            parts.append(Text(label, style=STYLES["skipped"]))
            index = end
            continue
        if step.state == "waiting" and index > anchor + 1:
            rest = sum(1 for item in steps[index:] if item.state == "waiting")
            parts.append(Text(f"等待({rest})", style=STYLES["waiting"]))
            break
        text = Text(NAMES.get(step.name, step.name), style=STYLES[step.state])
        if index == current:
            text.append(" (当前)", style=STYLES[step.state])
        parts.append(text)
        index += 1
    line = Text("流程脉络: ", style=STYLES["label"])
    for number, part in enumerate(parts):
        if number:
            line.append(" ── ", style=STYLES["decor"])
        line.append_text(part)
    line.no_wrap, line.overflow = True, "ellipsis"
    return line


def _details(snapshot: Snapshot, current: int | None) -> list[RenderableType]:
    """当前步与它前面最近执行的一步(没有当前步时为最后执行的两步)，再加之后第一个等待的步骤。"""
    steps = snapshot.steps
    executed = _executed(steps)
    rows: list[RenderableType] = []
    if executed:
        anchor = current if current is not None else executed[-1]
        focus = [*[index for index in executed if index < anchor][-1:], anchor]
        for number, index in enumerate(focus, start=1):
            if index == current:
                marker = Text("[当前步]", style=STYLES["running" if steps[index].state == "running" else "attention"])
            else:
                marker = Text("[上一步]" if current is not None or index != anchor else "[刚完成]",
                              style=STYLES["decor"])
            if rows:
                rows.append(Text(""))  # 步骤之间空一行
            rows += _step_rows(snapshot, number, steps[index], marker, index == current)
        later = next((index for index in range(anchor + 1, len(steps)) if steps[index].state == "waiting"), None)
        if later is not None:
            rows += [Text(""), _waiting_row(len(focus) + 1, steps[later])]
    rows += [Text("")] * max(0, DETAIL_LINES - len(rows))
    return rows[:DETAIL_LINES]


def _step_rows(snapshot: Snapshot, number: int, step: StepState, marker: Text, is_current: bool) -> list[Table]:
    model, note = _model_text(step.model), step.note
    if is_current and step.state == "running" and snapshot.agents:
        latest = snapshot.agents[-1]
        model = _model_text(_call_model(latest))
        marker = marker.copy().append("  已运行 ", style=STYLES["label"])
        marker.append(duration(snapshot.now - latest.started_at), style=STYLES["number"])
        note = "；".join(f"{agent.role} {agent.subject}" for agent in snapshot.agents)
    elif is_current and step.name == "unattended" and snapshot.issues:
        note = "；".join(_issue_note(issue.id, issue.steps) for issue in snapshot.issues)
    head = _grid(NAME_WIDTH, STATUS_WIDTH, DURATION_WIDTH, USAGE_WIDTH, None)
    head.add_row(_step_title(number, step.name), _pair("状态", _state(step.state)),
                 _pair("耗时", duration(step.duration), "number"), _pair("用量", tokens(step.tokens), "number"), marker)
    body = _grid(INDENT, MODEL_WIDTH, None)
    body.add_row("", _pair("模型", model), _pair("备注", note or "-"))
    return [head, body]


def _step_title(number: int, name: str) -> Text:
    """序号为装饰色，步骤名为标识色。"""
    title = Text(STEP_PREFIX.format(number=number), style=STYLES["decor"])
    return title.append(step_name(name), style=STYLES["ident"])


def _issue_note(issue_id: str, steps: Sequence[FixStep]) -> str:
    pending = next((step.label for step in steps if step.state not in ("done", "skipped")), None)
    return f"Issue {issue_id} {pending or '完成'}"


FIX_STATES = {"done": "done", "skipped": "skipped", "pending": "waiting", "blocked": "failed", "failed": "failed"}


def _fix_order(snapshot: Snapshot) -> list[ActiveIssue]:
    """正在调用模型的 Issue 排在前面。"""
    busy = {agent.subject for agent in snapshot.agents}
    return sorted(snapshot.issues, key=lambda issue: issue.id not in busy)


def _fix_current(issue: ActiveIssue) -> int | None:
    """第一个没有完成也没有跳过的修复步骤。"""
    return next((index for index, step in enumerate(issue.steps) if step.state not in ("done", "skipped")), None)


def _fix_streamline(issue: ActiveIssue, snapshot: Snapshot) -> Text:
    """Issue 的修复步骤：已完成的折叠为「已完成(N)」，当前步单列，之后的折叠为「等待(N)」。"""
    current = _fix_current(issue)
    line = Text("流程脉络: ", style=STYLES["label"]).append(f"Issue {issue.id} ", style=STYLES["ident"])
    finished = len(issue.steps) if current is None else current
    parts = [Text(f"已完成({finished})", style=STYLES["done"])] if finished else []
    if current is not None:
        step = issue.steps[current]
        busy = any(agent.subject == issue.id for agent in snapshot.agents)
        key = "running" if busy else FIX_STATES.get(step.state, "waiting")
        parts.append(Text(f"{step.label} (当前)", style=STYLES[key]))
        if len(issue.steps) - current - 1:
            parts.append(Text(f"等待({len(issue.steps) - current - 1})", style=STYLES["waiting"]))
    for number, part in enumerate(parts):
        if number:
            line.append(" ── ", style=STYLES["decor"])
        line.append_text(part)
    return _single(line)


def _fix_details(snapshot: Snapshot) -> list[RenderableType]:
    """每个进行中的 Issue 两行：当前步、状态、已运行时长、本次运行的用量与第几次调用；正在调用的模型与备注
    (上一次结束的调用的结果，或这一步记录的卡点)。"""
    rows: list[RenderableType] = []
    for number, issue in enumerate(_fix_order(snapshot)[:2], start=1):
        current = _fix_current(issue)
        step = issue.steps[current] if current is not None else None
        agents = [agent for agent in snapshot.agents if agent.subject == issue.id]
        if agents:
            state, started = Text("运行中", style=STYLES["running"]), duration(snapshot.now - agents[-1].started_at)
        else:
            key = FIX_STATES.get(step.state, "waiting") if step else "done"
            state, started = _state(key), "-"
        used = sum(call.tokens or 0 for call in issue.calls)
        role = agents[-1].role if agents else (issue.calls[-1].role if issue.calls else "")
        tries = sum(1 for call in issue.calls if call.role == role) + (1 if agents else 0)
        head = _grid(NAME_WIDTH, STATUS_WIDTH, DURATION_WIDTH, USAGE_WIDTH, None)
        title = Text(STEP_PREFIX.format(number=number), style=STYLES["decor"])
        title.append(f"Issue {issue.id} {step.label if step else '完成'}", style=STYLES["ident"])
        head.add_row(title, _pair("状态", state), _pair("已运行", started, "number"),
                     _pair("用量", tokens(used), "number"),
                     Text(f"{role} 第 {tries} 次" if role else "", style=STYLES["decor"]))
        body = _grid(INDENT, MODEL_WIDTH, None)
        model = _model_text(_call_model(agents[-1])) if agents else _model_text(
            issue.calls[-1].agent if issue.calls else None)
        body.add_row("", _pair("模型", model), _pair("备注", _fix_note(issue, step, role, bool(agents)) or "-"))
        if rows:
            rows.append(Text(""))
        rows += [head, body]
    rows += [Text("")] * max(0, DETAIL_LINES - len(rows))
    return rows[:DETAIL_LINES]


def _fix_note(issue: ActiveIssue, step: FixStep | None, role: str, busy: bool) -> str:
    """同一角色上一次结束的调用没有成功时写出结果(超时、格式不符等)，正在调用时注明在重试；否则为这一步记录的说明。"""
    finished = [call for call in issue.calls if call.role == role]
    if finished and finished[-1].result != "ok":
        call = finished[-1]
        label = RESULTS.get(call.result, (call.result, ""))[0]
        spent = duration(None if call.duration_ms is None else call.duration_ms / 1000)
        return f"上一次{label}({spent})" + ("，正在重试" if busy else "")
    return step.note if step else ""


def _waiting_row(number: int, step: StepState) -> Table:
    row = _grid(NAME_WIDTH, STATUS_WIDTH, DURATION_WIDTH, USAGE_WIDTH, None)
    row.add_row(_step_title(number, step.name), _pair("状态", _state("waiting")), "", "",
                Text("[下一步]", style=STYLES["decor"]))
    return row


def _logs(snapshot: Snapshot, zone: tzinfo | None) -> list[RenderableType]:
    """表头一行加最近 LOG_LINES 条事件；没有的行补空行，保持总高不变。"""
    grid = _grid(INDENT, *LOG_WIDTHS, None)
    grid.add_row("", *(Text(title, style=STYLES["label"]) for title in LOG_HEADERS))
    shown = snapshot.events[:LOG_LINES]
    for line in shown:
        label, style = RESULTS.get(line.result, (line.result, "running"))
        grid.add_row("", Text(line.at.astimezone(zone).strftime("%H:%M:%S"), style=STYLES["number"]),
                     Text(line.role, style=STYLES["ident"]), _model_text(line.agent or None),
                     Text(label, style=STYLES[style]),
                     Text("" if line.duration_ms is None else duration(line.duration_ms / 1000), style=STYLES["number"]),
                     Text("" if line.tokens is None else tokens(line.tokens), style=STYLES["number"]),
                     Text(line.note, style=STYLES["text"]))
    return [grid, *[Text("")] * (LOG_LINES - len(shown))]


def _footer(snapshot: Snapshot, interval: float) -> Text:
    line = Text("[q]", style=STYLES["decor"]).append(" 退出  ").append("[r]", style=STYLES["decor"])
    line.append(" 刷新 (").append(f"{interval:g}s", style=STYLES["number"]).append(")")
    loop = snapshot.loop
    if snapshot.paused:
        hint = "已暂停：tightrein resume --workspace <工作区> 恢复"
    elif loop is not None and loop.status is RunStatus.INTERRUPTED:
        hint = "下一步：tightrein run(接管中断的运行并继续)"
    elif loop is not None and loop.status is RunStatus.FAILED:
        hint = "下一步：tightrein status 查看失败的步骤与待处理事项"
    elif not snapshot.running:
        hint = "启动新一轮：tightrein run"
    else:
        hint = ""
    if hint:
        line.append("    ").append(hint, style=STYLES["running"])
    if snapshot.problems:
        line.append(f"    读取失败 {len(snapshot.problems)} 项：{snapshot.problems[0]}", style=STYLES["failed"])
    line.no_wrap, line.overflow = True, "ellipsis"
    return line
