# 恢复与控制(recovery)

## 是什么

中断后怎样接着做、写操作怎样不重复、运行怎样暂停与接管。程序在 `recovery.py`；幂等键的读写在 `store/tables/operations.py`，对账在 `git/`。

## 怎么做

### 检查点续跑

- 检查点就是每一步的 `handoff.json`(见 `handoff.md`)，写好才算这一步完成；
- 实施各步完成时，程序把 worktree 的 commit 记进必填事实的 `worktreeCommit`；
- 中断后从最后一个完成的步骤接着做(`last_checkpoint`)；没做完的那一步整个丢掉，worktree 退回上一个检查点的 commit(`rewind`；还没有检查点时退回开始时的 commit)，不续接半截状态。

检查点按写入时间排先后(只到秒)，同一秒内的按文件修改时间(纳秒)：编码第 2 轮(`35-…r2`)在审查第 1 轮(`37-…r1`)之后完成，只按文件名排会排错；按轮次再序号排时，不带轮次的交付(`38-…`)会排到同一秒的审查前面。

### 写操作幂等

- 提交、推送、提 PR、合并、发评论、同步 GitHub 都带幂等键：对象 + 步骤 + 操作 + 内容哈希，存在 store 的 operations 表；
- 先查键、再复核前置条件：执行前查该键是否做过，做过直接返回上次结果(已执行过的操作状态已变，先复核会误判为过期)；没做过的以做决定时观察到的状态(`expected`)重新观察，不同即过期(`git.Stale`)、不执行；
- 执行前写「进行中」，成功后改「已完成」并存结果；动作抛异常时删键，可以重试；
- 进程中途被杀时键停在「进行中」，再次执行抛 `InProgress`：先到远端确认实际状态(该提交是否已在、分支是否已推送、是否已有 PR)，做完了补记完成，没做删键重做。

### 中断时就地收尾

收到中断(Ctrl-C、SIGTERM、SIGHUP)：入口把信号转成 `Interrupted`(与 `KeyboardInterrupt` 同级，不会被 `except Exception` 吞掉)，沿调用栈抛出，沿途的 finally 与子进程组终止照常执行。命令结束时 `close_own`：

- 本进程开始、仍为进行中的运行：被中断的标为 `interrupted`，异常或模块漏了结束运行的标为 `failed`；
- 释放本进程持有的锁，每个运行写一条事件；
- 收尾在宽限期内做完(`within_grace`)，超过即强制结束；收尾本身出错不覆盖命令原来的结果，由下次启动恢复兜底。

退出码：Ctrl-C 为 130，信号为 128 + 编号(在 `cli/`)。

### 启动时恢复

每次运行开始时 `recover`：进行中的运行，心跳超过失效时限，或在本机且开始它的进程已不在，标为 `interrupted` 并写一条事件。运行记录插入时就写下进程号与主机，之后不改，判断不依赖锁；其他主机的只看心跳。同时调度恢复本机上持有进程已不在的只读 worktree 锁定(`git/worktrees.recover_readonly`，恢复失败保留标记，下次启动再试)。被中断的对象停在中断前的状态，从检查点接着做。没人管的 worktree 与残留进程由 `git/` 清理。

### 运行控制命令

| 命令 | 做什么 | 实现 |
|---|---|---|
| `tightrein pause` | 当前这一步做完后停下，不再开始新的步骤与对象；用户当场发起的命令不受影响 | `pause`：写 `data/control.json` |
| `tightrein stop` | 急停：立刻停掉所有工作，当前这一步退回上一个检查点 | `stop`：写 `data/control.json`，给本机进行中运行的进程发终止信号 |
| `tightrein resume` | 从暂停或急停恢复 | `resume`：删掉 `data/control.json` |
| `tightrein take <编号>` | 手动接管这个 Issue，tightrein 不再碰它 | `take`：issues 表的 `held_by` 记下接管者 |
| `tightrein give <编号>` | 交还给 tightrein，从检查点接着做 | `give`：清空 `held_by` |

调度在开始每一步与每个新对象前查 `control`，有暂停或急停就不再开始。

### 本机通知

不发。需要人看的都在三种文档(待审核、出问题、交付)里，用 watch 与 status 查看。

## 在哪配置

`settings/defaults.json`：

| 键 | 含义 |
|---|---|
| `limits.lock.heartbeat` | 心跳间隔 |
| `limits.lock.stale` | 多久没有心跳即判为失效 |
| `limits.shutdownGrace` | 中断收尾的宽限期 |

## 缺省值

| 项 | 缺省 | 出处 |
|---|---|---|
| 心跳间隔 | 30s | Temporal |
| 心跳失效 | 90s | 心跳间隔的 3 倍 |
| 中断收尾宽限 | 30s | Kubernetes 终止宽限期缺省 30s(Docker 为 10s) |
| 幂等键保留 | 30 天 | 推断：比 Stripe 的 24 小时长，流程可能停好几天(等审核、等额度重置) |

## 设计依据

- **检查点即 `handoff.json`**：每一步本来就要落盘这一份，续跑不再另记状态(44 号计划「恢复与控制」)。
- **不续接半截状态**：半截的 worktree 无法判断做到了哪里，退回上一个检查点重做这一步最可靠。
- **心跳代替按时间过期**：原来锁按「2 小时过期」，进程死了要等很久；改为心跳后最多 90 秒就能接管；本机按进程号判断是更快的路径(旧 `orchestrator/recovery.py`、`3879733`)。
- **信号转成 BaseException**：不会被各处的 `except Exception` 吞掉，finally 与进程组终止照常执行(旧 `cli/main.py:entry`、`3879733`)。
- **先查幂等键再复核前置条件**、**状态不明先对账**：旧 `vcs/executor.py:OperationRunner` 的做法。
- 出处：Temporal 心跳(temporal.io/blog/activity-timeouts)；Kubernetes 终止宽限期(kubernetes.io/docs/concepts/containers/container-lifecycle-hooks)；Stripe 幂等键(docs.stripe.com/api/idempotent_requests)；distributed-systems discipline for agentic flows(temporal.io/blog/from-ai-hype-to-durable-reality-why-agentic-flows-need-distributed-systems)。
