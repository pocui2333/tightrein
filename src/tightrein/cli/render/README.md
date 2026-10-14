# cli/render：status 与 watch 的取数与渲染

## 是什么

`tightrein status`(一次性快照)与 `tightrein watch`(实时界面)背后的程序：从 store 与工作区文件取数，渲染成固定行数、按显示宽度对齐的终端文字。命令壳在 `cli/commands/status.py`、`watch.py`。

| 文件 | 做什么 |
|---|---|
| `snapshot.py` | 取数：`status_snapshot(source) -> StatusSnapshot`、`watch_snapshot(source) -> WatchSnapshot`；`Source` 汇集要用的依赖 |
| `status.py` | `render_status(snapshot, language)`：不超过 20 行 |
| `watch.py` | `render_watch(snapshot, language, collect_view=…, width=…, height=…)`：行数等于 height(随终端高度) |
| `live.py` | `run(fetch, language, collect_view=…, on_key=…)`：按间隔重新取数、按当前终端宽高整屏重绘；按键 q、p、s、c |
| `style.py` | 颜色表、段(Span)与行、按显示宽度截断与补齐、框线与分节线、终端宽高 |
| `labels.py` | 两者共用的文字：调用点与来源名、时长与数量、时刻与相对时间 |

## 流程

命令壳组装 `Source`(工作区、连接、配置、时钟、本机主机名与进程存活判断) → 取数函数只读 store 与文件，返回 dataclass → 渲染函数按语言取文案表、拼成行 → 每行按显示宽度截断并补齐到同一宽度 → 终端且没有 `NO_COLOR` 时着色。watch 由 `live.run` 每 2s 调一次取数与渲染。

watch 自上而下：标题与时刻 → 运行(状态、阶段、开始、已跑、心跳) → Claude 与 agy 的额度(5h 与周，离留量线多少、何时重置) → 进行中(`WatchSnapshot.active`：实施中的每个 Issue、正在评估的问题、运行级的采集与复盘各一行：编号与 GitHub 编号、标题、步骤进度条、当前步骤、在这一步多久、这次调用读入的 token、第几轮、模型、Issue 的 token 预算) → 需要你处理(只在有事时出现：运行中断、暂停或急停、待审核与出问题的 Issue、评估转人工的问题；右边是要敲的命令) → 最近事件(行数随终端高度) → 排队 → 按键。按 [c] 时「进行中」换成采集看板。

## 输入与输出

读的东西(只读，不调用模型，不写任何记录)：

- store：`runs`(当前、上次、中断的运行)、`issues`(等你处理、进行中、排队、接管、发布；`extra.github.number` 为 GitHub 编号)、`problems`(按状态计数；`extra.verdict`、`extra.severity` 计评估判定与严重度；`extra.assess.manual` 为评估转人工)、`counters`(`issue_tokens.<编号>`、`breaker.dependency.*`)、`state`(`quota`、`schedule.missed`、`knowledge.pending`、`knowledge.stale`)；
- 文件：对象目录的 `*-handoff.json`(经 `protocol.recovery.checkpoints`)、`90-*-pending.md`、`90-*-failure.md`(只看存在与修改时间)、agents 写的 `*-started.json`(`endedAt` 为空即调用正在进行)、运行目录的 `events.jsonl`、`setup.json`、`control.json`、`data/retro/*.md`(文件名中的评级与开头的 `status:` 行)。

各阶段写进 handoff 必填事实、给这里读的键集中在 `snapshot.py` 开头的 `FACT_*`、`EXTRA_*`、`STATE_*` 常量：跳过的原因 `skipped`、定案是否自动 `auto`、审查阻断项 `blockers`(`location`、`kind`、`summary`)、编码的 `diffHash`、`linesAdded`、`linesDeleted`、采集来源的 `read` 与 `metrics.produced.signals`、去重的 `new`、`merged`、`muted`、`regressed`。

输出：渲染后的字符串(行用 `\n` 连接，不带末尾换行)。

## 配置

不新增配置。显示用到的上限与门槛都取自已有取值：`resources.issueTokens`、`resources.quota.reserve*`、各控制键的 `timeout`、`turns`、`rounds`、`schedule.tick` 与 `schedule.window`、`limits.lock.stale`、`limits.breaker.*`、`records.retention.runs`。语言取项目的 `project.language`，文字取 `cli/text/<语言>.json`。

## 设计依据

- **只读、不调用模型、不写记录**：watch 随时可开可关，不影响运行(旧 `monitor/snapshot.py` 的模块约定)。
- **进程已不在的运行显示为中断，并给出接管命令**：同主机且开始它的进程已不在，或心跳超过 `limits.lock.stale`，按中断显示，提示 `tightrein run`(启动恢复会接管并从检查点接着做)；否则会显示成一直在运行或失败(旧 `monitor/snapshot.py:take` 的 gone)。
- **当前步骤、第几轮与上一次调用失败的原因**：started 标记 `endedAt` 为空即调用进行中；这一步上一次结束的调用没成功时写出结果并标「重试中」，模型重试或超时不会看起来像卡死(旧 `monitor/view.py:_fix_details`、`_fix_note`，#5)。
- **并行的对象各占一行**：实施按 `resources.concurrency.sessions` 同时推进几个 Issue，评估按组并行；只给一个「当前对象」时其余几个像没在动。每行只放判断「在不在动、卡没卡、花了多少」要的东西，细节用 `tightrein show <编号> --steps` 看。
- **需要你处理只在有事时出现**：平时让出来的行给事件流水；出现时每条右边就是要敲的命令。
- **行数随终端高度、超长截断、每行补齐到同一宽度**：同一终端大小下每次刷新行数相同，不滚屏不抖动；窗口拉高时事件流水多几行，太矮时按最低行数排(`style.MIN_HEIGHT`)；放不下的进行中与需要你处理的条目在最后一行写「另 N 个」。
- **事件流水分轻重**：一步的结果(passed、failed、pending、returned)按结果标出，按流程退回(returned：重出方案、等前置 Issue、拆分)不标成出错；模型调用成功是例行的，整行淡显；agents 每次调用都记的「子进程环境去掉了」不列。
- **颜色**：浅色与深色背景都要看得清。普通文字不着色(终端的缺省前景色)，数值只加粗；状态用 16 色的绿、青、黄、红，跟着终端配色方案走；辅助文字与框线用 256 色中灰 244。不用 256 色的近白(浅色背景上看不见)，也不用暗蓝(ANSI 4，深色背景上看不见)；状态、数值、命令加粗，命令加下划线；非终端或 `NO_COLOR`(https://no-color.org)时不着色。
- **按显示宽度对齐**：中文等宽字符占两格(`unicodedata.east_asian_width` 为 W、F)，宽度不定(A)的符号按一格，与 macOS 终端缺省一致。
- **没有进展的判断与停下用同一处**：进行中一行的「与上轮比无进展」直接调用 `protocol.limits.no_progress`(审查至少交回两轮时才比)，与调度停下的规则一致。
- **不引第三方库**：旧版用 rich；这里只需定宽行与颜色，标准库足够(实现原则 6)。

## 不做什么

- 不写运行控制：watch 的 [p]、[s] 交给命令壳调用 `protocol.recovery.pause`、`stop`；
- 不判断对象该怎么处理、不推算调度：下次定时只按 `schedule.tick` 与 `schedule.window` 推算显示用的时刻；
- 不解析交接的 Markdown、PR 正文等其他文件。
