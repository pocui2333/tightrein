# 调度(schedule)

## 是什么

什么时候跑、跑什么、跑到哪一步停。规则在这里，执行的程序在 `schedule/`；取值(多久醒一次、每个来源的间隔、能跑的时段、自动推进到哪一步)在 settings。

## 怎么做

### 三种触发

- **定时**：只设一个定时器(macOS 为 launchd)按固定间隔醒来，查 store 的 state 表看哪些来源到点，只跑到点的；
- **事件**：有新部署或新提交时，跑依赖代码的来源(静态巡检、API 模糊测试)；
- **手动**：命令触发。

到期判断：错过多个时刻只补跑一次并记下错过次数；从未跑过的立即跑；失败或中断的运行不算「已跑过」，下次醒来重新触发。

### 按状态推进

每次运行把每个对象往前推一步，到人工关卡就停；顺序为 采集(含去重) → 评估 → 实施 → 发布(含验收) → 复盘。调度只看对象处于什么状态，不判断对象该怎么处理。

- 每一步包在独立的错误边界中：一步出错只记为该步失败，后面的步骤与其他对象照常执行；
- 暂停、急停与手动接管按 `recovery.md`：有暂停或急停时不再开始新的步骤与对象，`held_by` 有值的 Issue 不碰；
- 对象熔断、没有进展就停按 `limits.md`。

### 同一时间只跑一次

运行锁(文件锁加心跳)保证；上一次没跑完时这一次直接跳过，不等待，记一条「跳过」。

### 每次运行结束都执行复盘

### launchd

plist 中路径一律绝对(launchd 不经 shell、不展开 `~`)；PATH 以 tightrein 所在目录开头并去重，显式设 LANG；用户级 `gui/<uid>` 域；已存在时先 bootout 再 bootstrap；launchctl 失败时保留 plist 便于排查；日志写进工作区并按大小轮转。

## 在哪配置

`settings/defaults.json` 的 `schedule`(可在 `controls.json` 与工作区 `settings.json` 覆盖)：

| 键 | 含义 |
|---|---|
| `schedule.tick` | 定时器多久醒一次 |
| `schedule.window` | 能跑的时段(`from`、`to`)与星期(`days`) |
| `schedule.every.<采集模块>` | 每个来源的间隔；`on_deploy`、`on_commit` 为事件触发 |
| `schedule.advanceTo` | 无人值守自动推进到哪个阶段为止 |
| `schedule.logs` | launchd 日志按大小轮转：`maxBytes` 超过即改名，留 `keep` 份 |

## 缺省值

| 项 | 缺省 |
|---|---|
| 醒来间隔 | 15m |
| 能跑的时段 | 每天 00:00 到 07:00 |
| 项目探针、平台错误、任务外发现 | 每 1h |
| 访问日志 | 每 1d |
| 业务告警 | 每 15m |
| API 模糊测试 | 有新部署时 |
| 静态巡检 | 有新提交时 |
| 自动推进到 | release |
| launchd 日志 | 超过 5MB 轮转，留 3 份 |

## 设计依据

- **只设一个定时器、按 state 表判断到点**：来源多了也只有一个 launchd 任务，间隔改配置即生效，不用重装(44 号计划「调度」)。
- **调度只看状态**：怎么处理对象是各阶段的事，调度不重复判断，避免两处规则对不上。
- **运行锁不等待**：定时运行重叠时排队只会越积越多，跳过并记下即可(旧 `orchestrator/service.py`、`orchestrator/schedule.py:skipped`)。
- **每一步独立的错误边界**：一个来源或一个对象的失败不拖垮整次运行(旧 `orchestrator/rules.py:run_steps`)。
- **错过的只补一次**：机器睡眠醒来后不连跑多次(旧 `orchestrator/schedule.py:_due`)。
- launchd：man launchd.plist；`launchctl bootstrap/bootout`(man launchctl)。
