# cli：命令行

## 是什么

只解析命令与显示；数据都从 store 与交接文件读，自己不存东西。入口 `tightrein.cli.main:entry`；`assemble.py` 是全仓库唯一接触真实外部依赖(环境变量、子进程、时钟、进程号、家目录、终端)的地方，拼好 `Runtime` 交给调度与各阶段。

## 命令一览

### 日常(顶层)

| 命令 | 做什么 |
|---|---|
| `tightrein` / `tightrein status [--json]` | 一次性快照；不带命令时默认执行 |
| `watch [--collect]` | 实时界面；`--collect` 直接打开采集看板；界面里 `p` 暂停、`s` 急停、`c` 切换、`q` 退出 |
| `show <编号> [--steps] [--doc pending/failure/deliver]` | 一个问题或 Issue 的详情：每一步的结论(`--steps` 加量化数据)、三种文档的路径；`--doc` 直接打印该文档 |
| `show --search "<关键词>"` | 按标题与位置查找问题与 Issue |
| `approve <编号> [--option <序号>] [--note "<补充>"]` | 通过待审核的事项；要拍板的用 `--option` 选 |
| `reject <编号> --note "<原因>"` | 不通过；方案按原因重出，原因必填 |
| `new "<需求描述>" [--severity P0..P3] [--type bug/feature]` | 用户自己提需求，直接进评估 |
| `run [collect/assess/implement/release/retro] [--object <编号>] [--dry-run]` | 手动触发：不带阶段按状态推进一轮；带阶段只跑该阶段；`--object` 只推进一个对象；`--dry-run` 只列出会做什么 |
| `pause [--note]` | 当前一步做完后停下 |
| `stop [--note]` | 急停：立刻停掉，当前一步退回检查点 |
| `resume` | 从暂停或急停恢复 |
| `take <编号>` | 手动接管，tightrein 不再碰它 |
| `give <编号>` | 交还 tightrein，从检查点接着做 |

### 分组

| 分组 | 子命令 |
|---|---|
| `project` | `add <仓库路径> [--name <名>]`；`check [<模块>]`；`ready`；`show`；`config [<键>] [--explain]`；`list`；`remove <名>` |
| `problem` | `list [--status <状态>]`；`mute <编号> --days N --note "<原因>"`；`unmute <编号>`；`reopen <编号>` |
| `retro` | `list [--rating P0..P3] [--all]`；`show <编号>`；`close <编号> --done 或 --wontfix` |
| `knowledge` | `list [--pending 或 --stale]`；`show <编号>`；`confirm <编号>`；`drop <编号> [--note]`；`add --kind … --slug … --title … --summary … --body … [--location …]` |
| `admin` | `install [--tool claude/agy/codex] [--dry-run]`；`uninstall [--tool …] [--dry-run]`；`rebuild`；`check`；`clean [--dry-run]` |

取消的老命令(`continue`、`find`、单步的 `collect`、`triage`、`fix`、`verify`、`release`、`learn`、`issue` 分组等)敲出来时报用法错误并给出新写法，不执行(`main.RENAMED`)。

## 通用规则

- 全局选项写在命令之后：`-p, --project <名>`(缺省取当前目录所在的工作区或项目仓库；只有一个项目时取它)；`--json`(标准输出只写一个 JSON 对象：`command`、`status`、`exitCode`、`result`、`next`、`errors`，参数错误也一样)；`--lang zh/en`(缺省取项目的输出语言，文案表没有的语言取英文)；`--yes`(跳过确认)；`-h, --help`；
- 编号直接写 `0019`(或 `19`)，不加 `#`；问题与 Issue 编号不重叠，自动识别；
- 会改动东西的命令先列出要做什么，问 `y/N`；`--yes` 跳过；非交互(`--json` 或标准输入不是终端)又没给 `--yes` 时报用法错误，不当成同意。`run` 与 `project check` 不问(要先看就用 `run --dry-run`)；
- 人读输出第一行是结论，随后是明细，最后一行是能直接复制执行的下一步命令；
- 帮助与输出文字取 `text/zh.json`、`en.json`，用 `text(language, "<区块>.<名>", **值)` 取；
- 异常按类型映射退出码，不向终端抛堆栈；
- 中断：入口把 SIGTERM、SIGHUP 转成 `protocol.process.Interrupted`，沿调用栈抛出，沿途 finally 与子进程组终止照常执行；命令结束时就地收尾(`protocol.recovery.close_own`)：本进程开始、仍在进行的运行标为中断(Ctrl-C、信号)或失败(异常、模块漏了结束运行)，释放本进程持有的锁；收尾在 `limits.shutdownGrace` 内做完，收尾出错不覆盖命令原来的结果，由下次启动恢复兜底。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 失败(有步骤失败、试跑不通过、安装检查不通过、未映射的异常) |
| 2 | 用法错误(参数、项目名、编号不合法；配置或接入清单不合格；对象不存在) |
| 3 | 被拒绝执行：运行锁被占、处于暂停或急停、订阅额度停机 |
| 4 | 停在人工关卡(`run` 结束时有待审核) |
| 130 | Ctrl-C |
| 128 + 编号 | 被信号终止(SIGTERM 143、SIGHUP 129) |

## 目录

```
cli/
  main.py          解析、分发、输出与收尾；entry 安装信号处理
  session.py       一条命令的执行环境(Session)、结果(Result)、确认与输出
  assemble.py      外部依赖(Externals)、项目解析、Runtime 组装
  exit_codes.py    退出码与异常映射
  commands/        每个日常命令或分组一个文件(status、watch 的取数与渲染在 render/)
  render/          status、watch 的取数与渲染
  text/            zh.json、en.json 文案表与 text()
```

## 设计依据

- 信号转成 BaseException、只在 entry 安装、退出码 130 与 128+编号、命令结束就地收尾：旧 `cli/main.py:entry`、`Invocation._close_own_runs`(`3879733`)；
- `--json` 只写一个对象、异常映射退出码：旧 `cli/output.py:emit`、`cli/exit_codes.py:for_error`；
- 改了名的老命令报用法错误并给出新写法：旧 `cli/main.py:RENAMED`；
- 组装根唯一接触外部依赖：旧 `cli/assemble.py:App`，测试整体替换 `Externals`。

## 不做什么

- 不存任何状态(都在 store 与工作区文件)；
- 不在命令里判断对象该怎么处理(调度与各阶段的事)；
- 不发本机通知。
