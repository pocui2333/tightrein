# schedule：调度

## 是什么

什么时候跑、跑什么、跑到哪一步停的执行程序；规则写在上一层的 `protocol/schedule.md`，取值在 settings 的 `schedule`。入口 `run(runtime, *, trigger, stage=None, subject=None, dry_run=False) -> Outcome`，由 `tightrein run` 调用(launchd 定时调用时带 `--trigger schedule`)。

## 流程

1. 暂停、急停(`protocol/recovery.control`)或订阅额度用完(`Quota.halted_until`)时不开始：`Outcome.status = refused`，命令行退出码 3；
2. 定时与事件触发：项目没有就绪(state `onboard.ready`)、定时触发不在能跑的时段(`schedule.window`，本机时区，跨午夜算在开始那天)时跳过，不记运行；
3. `--dry-run`：按当前状态列出会做的事(到点的来源、待评估的问题、要实施的 Issue、发布中的 Issue、复盘)，不取锁、不写任何记录；
4. 运行锁(`store/locks.FileLock` 于 `WorkspaceLayout.run_lock`)不等待：被占即返回 refused；定时与事件触发记一条状态为 skipped 的运行；
5. 登记运行(runs 表)，后台线程每隔 `limits.lock.heartbeat` 续锁与 runs 的心跳；锁被接管时在下一步之前停下；
6. 启动恢复(`recovery.recover`；持有进程已不在的只读 worktree 锁定由 `git/worktrees.recover_readonly` 恢复写权限，失败保留标记下次再试)→ 保留期清理(`store/retention.purge`，`records.retention`)→ 定时触发时轮转 launchd 日志；
7. 按顺序推进，每一步与每个对象的每一步都包在独立的错误边界里，开始每一步、每个新对象前查暂停：
   - 采集：`collect.collect.run`；手动触发时把启用的来源全部交给它(`only`)，其余由采集按 state 表判断到点；
   - 评估：指定了问题时 `assess.assess.assess`，否则 `assess.assess.assess_pending`；额度到了留余量的门槛时不开始；
   - 实施：同时只处理一个 Issue：有 implementing 的先做它，没有才按严重度、编号开始一个 todo 的(留余量时不开始新的)；`implement.implement.implement` 一步一步推，停在关卡(pending)、失败、离开实施或推进后没有前进为止；
   - 发布：releasing 的 Issue 只交给合并队列 `release.queue.queue`(它定先后与合并)；accepting 的(验收观察中)与指定的对象单独调用 `release.release.release`，一次推进到能走多远就走多远(等 CI、等部署、观察期、要人决定时停)，同一次运行中不重复调用(队列里刚合并转入验收的也不再调用)；
   - 复盘：`retro.retro.retro`，每次运行结束都跑；
8. 结束：有步骤失败为 failed，否则 done；摘要(每一步的阶段、对象、状态、一句话)写进 runs 的 summary。

定时与事件触发只自动推进到 `schedule.advanceTo`(缺省 release)，复盘总是最后跑；指定阶段只跑该阶段；指定对象时问题只跑评估、Issue 只跑实施与发布。

### 对象熔断

一个对象的一步失败，或推进后(调用点, 下一步)与上一步相同(没有前进)，`Breaker.object_failed` 记一次；到 `limits.breaker.objectFailures` 次即停止处理：Issue 经状态机 TAKE 由 `tightrein:breaker` 接管(`tightrein give` 交还)，计数清零。成功推进一步即清零；停在人工关卡不计数。熔断已打开的对象跳过。

### launchd(launchd.py)

每个项目一个用户级 LaunchAgent `local.tightrein.<项目>`，按 `schedule.tick` 醒来：能整除一小时或一天的写成从零点对齐的 `StartCalendarInterval`(机器睡眠错过的时刻醒来后只补一次；`next_run` 推算的下次时刻与实际一致)，其余写 `StartInterval`；plist 中路径一律绝对；`PATH` 以 tightrein 所在目录开头、接上安装时的 PATH 并去重；显式设 `LANG` 与 `TIGHTREIN_HOME`；`gui/<uid>` 域，已存在时先 bootout 再 bootstrap；launchctl 失败时保留 plist；日志写进工作区 `data/logs/`，每次定时运行开始时超过 `schedule.logs.maxBytes` 即改名轮转，留 `keep` 份。由 `tightrein project ready` 安装、`project remove` 卸下。

### 给 status 与 watch 的

- `in_window(settings, now)`：是否在能跑的时段；`next_run(settings, now)`：下一次在时段内醒来的时刻；
- state 表 `schedule.missed`：上次定时醒来之前漏掉了几次醒来(按 `schedule.tick` 与 `schedule.lastWake` 算，机器睡眠或定时器没装好时大于 0)。

## 输入与输出

- 输入：`Runtime`(cli/assemble.py 组装)、触发方式(schedule、event、manual)、阶段、对象；
- 输出：`Outcome(run, status, reason, steps, planned, halted)`；`pending` 为真表示有对象停在人工关卡(命令行退出码 4)；
- 记录：runs 表一行、运行目录的 events.jsonl(启动恢复、清理、停下、失败、熔断)。

## 配置

`settings/defaults.json` 的 `schedule`(tick、window、every、advanceTo、logs)、`limits.lock`、`limits.breaker.objectFailures`、`records.retention`、`resources.quota`。

## 设计依据

- 运行锁不等待、定时运行记一条跳过：旧 `orchestrator/service.py:Orchestrator.run`、`orchestrator/schedule.py:skipped`；
- 每一步独立的错误边界：旧 `orchestrator/rules.py:run_steps`；
- 暂停在当前步骤后生效：旧 `orchestrator/rules.py:RunContext.halting`；
- 对象熔断：旧 `orchestrator/breaker.py`；
- 到点判断(错过只补一次、失败的不算已跑、新来源立即跑)在采集里实现(`collect/collect.py:select`)，上次时间与读取位置同一个事务保存；
- launchd：旧 `packaging/launchd.py`；man launchd.plist、man launchctl。

## 不做什么

- 不判断对象该怎么处理(那是各阶段的事)，只看状态；
- 不发本机通知(需要人看的都在三种文档里，用 watch 与 status 查看)；
- 不同时处理多个 Issue 的实施。
