# cli/render：status 与 watch 的取数与渲染

## 是什么

`tightrein status`(一次性快照)与 `tightrein watch`(实时界面)背后的程序：从 store 与工作区文件取数，渲染成固定行数、按显示宽度对齐的终端文字。命令壳在 `cli/commands/status.py`、`watch.py`。

| 文件 | 做什么 |
|---|---|
| `snapshot.py` | 取数：`status_snapshot(source) -> StatusSnapshot`、`watch_snapshot(source) -> WatchSnapshot`；`Source` 汇集要用的依赖 |
| `status.py` | `render_status(snapshot, language)`：不超过 20 行 |
| `watch.py` | `render_watch(snapshot, language, collect_view=…)`：固定 16 行 |
| `live.py` | `run(fetch, language, collect_view=…, on_key=…)`：按间隔重新取数、整屏重绘；按键 q、p、s、c |
| `style.py` | 颜色表、段(Span)与行、按显示宽度截断与补齐、框线 |
| `labels.py` | 两者共用的文字：调用点与来源名、时长与数量、时刻与相对时间 |

## 流程

命令壳组装 `Source`(工作区、连接、配置、时钟、本机主机名与进程存活判断) → 取数函数只读 store 与文件，返回 dataclass → 渲染函数按语言取文案表、拼成行 → 每行按显示宽度截断并补齐到同一宽度 → 终端且没有 `NO_COLOR` 时着色。watch 由 `live.run` 每 2s 调一次取数与渲染。

## 输入与输出

读的东西(只读，不调用模型，不写任何记录)：

- store：`runs`(当前、上次、中断的运行)、`issues`(等你处理、进行中、排队、接管、发布)、`problems`(按状态计数；`extra.verdict`、`extra.severity` 计评估判定与严重度)、`counters`(`issue_tokens.<编号>`、`breaker.dependency.*`)、`state`(`quota`、`schedule.missed`、`knowledge.pending`、`knowledge.stale`)；
- 文件：对象目录的 `*-handoff.json`(经 `protocol.recovery.checkpoints`)、`90-*-pending.md`、`90-*-failure.md`(只看存在与修改时间)、agents 写的 `*-started.json`(`endedAt` 为空即调用正在进行)、运行目录的 `events.jsonl`、`setup.json`、`control.json`、`data/retro/*.md`(文件名中的评级与开头的 `status:` 行)。

各阶段写进 handoff 必填事实、给这里读的键集中在 `snapshot.py` 开头的 `FACT_*`、`EXTRA_*`、`STATE_*` 常量：跳过的原因 `skipped`、定案是否自动 `auto`、审查阻断项 `blockers`(`location`、`kind`、`summary`)、编码的 `diffHash`、`linesAdded`、`linesDeleted`、命中的 `highRiskPaths`、采集来源的 `read` 与 `metrics.produced.signals`、去重的 `new`、`merged`、`muted`、`regressed`。

输出：渲染后的字符串(行用 `\n` 连接，不带末尾换行)。

## 配置

不新增配置。显示用到的上限与门槛都取自已有取值：`resources.issueTokens`、`resources.quota.reserve*`、`boundaries.changeCap`、各控制键的 `timeout`、`turns`、`rounds`、`schedule.tick` 与 `schedule.window`、`limits.lock.stale`、`limits.breaker.*`、`records.retention.runs`。语言取项目的 `project.language`，文字取 `cli/text/<语言>.json`。

## 设计依据

- **只读、不调用模型、不写记录**：watch 随时可开可关，不影响运行(旧 `monitor/snapshot.py` 的模块约定)。
- **进程已不在的运行显示为中断，并给出接管命令**：同主机且开始它的进程已不在，或心跳超过 `limits.lock.stale`，按中断显示，提示 `tightrein run`(启动恢复会接管并从检查点接着做)；否则会显示成一直在运行或失败(旧 `monitor/snapshot.py:take` 的 gone)。
- **当前步骤、第几轮与上一次调用失败的原因**：started 标记 `endedAt` 为空即调用进行中；这一步上一次结束的调用没成功时写出结果并标「重试中」，模型重试或超时不会看起来像卡死(旧 `monitor/view.py:_fix_details`、`_fix_note`，#5)。
- **总高固定、超长截断、每行补齐到同一宽度**：刷新时不滚屏不抖动，宽窄终端都能放下；改用 44 号计划「定下的样式」的卡片外框，行数不变(旧 `monitor/view.py:render`)。
- **颜色**：ANSI 256 色，不用暗蓝(ANSI 4)；状态、数值、命令加粗，命令加下划线；非终端或 `NO_COLOR`(https://no-color.org)时不着色。
- **按显示宽度对齐**：中文等宽字符占两格(`unicodedata.east_asian_width` 为 W、F)，宽度不定(A)的符号按一格，与 macOS 终端缺省一致。
- **没有进展的判断与停下用同一处**：卡片上「进展」直接调用 `protocol.limits.no_progress`，与调度停下的规则一致。
- **不引第三方库**：旧版用 rich；这里只需定宽行与颜色，标准库足够(实现原则 6)。

## 不做什么

- 不写运行控制：watch 的 [p]、[s] 交给命令壳调用 `protocol.recovery.pause`、`stop`；
- 不判断对象该怎么处理、不推算调度：下次定时只按 `schedule.tick` 与 `schedule.window` 推算显示用的时刻；
- 不解析交接的 Markdown、PR 正文等其他文件。
