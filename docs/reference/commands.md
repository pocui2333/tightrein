# 命令

> 本文件由 `src/tightrein/cli/reference.py` 生成，不要手改；改了命令或帮助文字后运行 `python -m tightrein.cli.reference`。

命令的通用规则(编号写法、确认、`--json` 的输出)与退出码见 `src/tightrein/cli/README.md`。

## 全局选项

每个命令都接受，写在命令之后：

| 选项 | 说明 |
|---|---|
| `-h, --help` | 显示帮助 |
| `-p, --project <project>` | 项目名；缺省取当前目录所在的工作区，只有一个项目时取它 |
| `--json` | 以一个 JSON 对象输出 |
| `--lang {zh,en}` | 输出语言；缺省取项目的输出语言 |
| `--yes` | 跳过确认 |

不带命令时执行 `tightrein status`。

## 一览

| 命令 | 做什么 |
|---|---|
| `tightrein status` | 一次性快照(不带命令时默认执行) |
| `tightrein watch` | 实时界面 |
| `tightrein show` | 一个问题或 Issue 的详情 |
| `tightrein approve` | 通过待审核的事项 |
| `tightrein reject` | 不通过；方案按原因重出 |
| `tightrein new` | 自己提需求，直接进评估 |
| `tightrein run` | 手动触发：不带阶段按状态推进一轮 |
| `tightrein pause` | 当前一步做完后停下 |
| `tightrein stop` | 急停：立刻停掉，当前一步退回检查点 |
| `tightrein resume` | 从暂停或急停恢复 |
| `tightrein take` | 手动接管，tightrein 不再碰它 |
| `tightrein give` | 交还 tightrein，从检查点接着做 |
| `tightrein project` | 接入项目 |
| `tightrein project add` | 建工作区并探测仓库 |
| `tightrein project check` | 试跑启用与自定义的模块 |
| `tightrein project ready` | 试跑全部通过后标为就绪并装上定时器 |
| `tightrein project show` | 看接入清单 |
| `tightrein project config` | 看配置的取值 |
| `tightrein project list` | 列出所有项目 |
| `tightrein project remove` | 删除工作区(不动仓库) |
| `tightrein problem` | 查看与处置问题 |
| `tightrein problem list` | 列出问题 |
| `tightrein problem mute` | 抑制一个问题 |
| `tightrein problem unmute` | 取消抑制 |
| `tightrein problem reopen` | 重新打开已关闭或已解决的问题 |
| `tightrein retro` | tightrein 自身问题的记录簿 |
| `tightrein retro list` | 按评级列出待看的记录 |
| `tightrein retro show` | 看一条记录 |
| `tightrein retro close` | 关闭一条记录 |
| `tightrein knowledge` | 项目的知识库 |
| `tightrein knowledge list` | 列出条目 |
| `tightrein knowledge show` | 看一条条目或建议 |
| `tightrein knowledge confirm` | 确认一条建议并写成条目 |
| `tightrein knowledge drop` | 放弃一条建议 |
| `tightrein knowledge add` | 手写一条条目 |
| `tightrein admin` | 本机的安装、自检与维护 |
| `tightrein admin install` | 装到 agent 工具(先检查后执行) |
| `tightrein admin uninstall` | 卸下(只删记录过的) |
| `tightrein admin rebuild` | 从文件重建数据库 |
| `tightrein admin check` | 自检 |
| `tightrein admin clean` | 立即按保留期清理 |

## 日常命令

### status

一次性快照(不带命令时默认执行)

```
tightrein status
```

### watch

实时界面

```
tightrein watch [--collect]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--collect` | 否 | — | 直接打开采集看板 |

### show

一个问题或 Issue 的详情

```
tightrein show [<id>] [--steps] [--doc {pending,failure,deliver}] [--search <text>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 否 | — | 问题或 Issue 编号，如 19 或 P-0003(不加 #) |
| `--steps` | 否 | — | 列出每一步的量化数据 |
| `--doc {pending,failure,deliver}` | 否 | — | 直接打印该文档 |
| `--search <text>` | 否 | — | 按描述查找问题与 Issue |

### approve

通过待审核的事项

```
tightrein approve <id> [--option <n>] [--note <note>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | Issue 编号，如 19 |
| `--option <n>` | 否 | — | 要拍板时选第几个选项 |
| `--note <note>` | 否 | — | 补充说明 |

### reject

不通过；方案按原因重出

```
tightrein reject <id> --note <note>
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | Issue 编号，如 19 |
| `--note <note>` | 是 | — | 不通过的原因(必填) |

### new

自己提需求，直接进评估

```
tightrein new <text> [--severity {P0,P1,P2,P3}] [--type {bug,feature}]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<text>` | 是 | — | 需求描述 |
| `--severity {P0,P1,P2,P3}` | 否 | — | 严重度 |
| `--type {bug,feature}` | 否 | `feature` | 类型 |

### run

手动触发：不带阶段按状态推进一轮

```
tightrein run [<stage>] [--object <id>] [--dry-run] [--debug]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<stage>` | 否 | — | 只跑这个阶段；取值 collect, assess, implement, release, retro |
| `--object <id>` | 否 | — | 只推进这个对象 |
| `--dry-run` | 否 | — | 只列出要做什么，不执行 |
| `--debug` | 否 | — | 成功的模型调用也保存 prompt 与 raw(重新录制、排查提示用) |

### pause

当前一步做完后停下

```
tightrein pause [--note <note>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--note <note>` | 否 | — | 补充说明 |

### stop

急停：立刻停掉，当前一步退回检查点

```
tightrein stop [--note <note>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--note <note>` | 否 | — | 补充说明 |

### resume

从暂停或急停恢复

```
tightrein resume
```

### take

手动接管，tightrein 不再碰它

```
tightrein take <id>
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | Issue 编号，如 19 |

### give

交还 tightrein，从检查点接着做

```
tightrein give <id>
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | Issue 编号，如 19 |

## project

接入项目

### project add

建工作区并探测仓库

```
tightrein project add <path> [--name <name>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<path>` | 是 | — | 要接入的仓库目录 |
| `--name <name>` | 否 | — | 工作区名；缺省取仓库目录名 |

### project check

试跑启用与自定义的模块

```
tightrein project check [<module>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<module>` | 否 | — | 只试跑这个模块(接入清单中的键，如 collect.static)；缺省试跑全部并跑基线 |

### project ready

试跑全部通过后标为就绪并装上定时器

```
tightrein project ready
```

### project show

看接入清单

```
tightrein project show
```

### project config

看配置的取值

```
tightrein project config [<key>] [--explain]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<key>` | 否 | — | 配置键，如 language 或 controls.implement.code.turns；缺省输出全部 |
| `--explain` | 否 | — | 列出每一层给出的值 |

### project list

列出所有项目

```
tightrein project list
```

### project remove

删除工作区(不动仓库)

```
tightrein project remove <name>
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<name>` | 是 | — | 要删除的工作区名 |

## problem

查看与处置问题

### problem list

列出问题

```
tightrein problem list [--status {pending,watching,new,ongoing,intermittent,resolved,regressed,muted,closed}]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--status {pending,watching,new,ongoing,intermittent,resolved,regressed,muted,closed}` | 否 | — | 只列这个状态的问题 |

### problem mute

抑制一个问题

```
tightrein problem mute <id> --days <days> --note <note>
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | 问题编号，如 P-0003 |
| `--days <days>` | 是 | — | 抑制多少天 |
| `--note <note>` | 是 | — | 补充说明 |

### problem unmute

取消抑制

```
tightrein problem unmute <id> [--note <note>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | 问题编号，如 P-0003 |
| `--note <note>` | 否 | — | 补充说明 |

### problem reopen

重新打开已关闭或已解决的问题

```
tightrein problem reopen <id> [--note <note>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | 问题编号，如 P-0003 |
| `--note <note>` | 否 | — | 补充说明 |

## retro

tightrein 自身问题的记录簿

### retro list

按评级列出待看的记录

```
tightrein retro list [--rating {P0,P1,P2,P3}] [--all]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--rating {P0,P1,P2,P3}` | 否 | — | — |
| `--all` | 否 | — | 含已处理与不处理的 |

### retro show

看一条记录

```
tightrein retro show <id>
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | 记录编号，如 3 |

### retro close

关闭一条记录

```
tightrein retro close <id> (--done | --wontfix)
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | 记录编号，如 3 |
| `--done` | 是 | — | 已处理(与 `--wontfix` 必选其一) |
| `--wontfix` | 是 | — | 不处理(与 `--done` 必选其一) |

## knowledge

项目的知识库

### knowledge list

列出条目

```
tightrein knowledge list [--pending | --stale]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--pending` | 否 | — | 列出待确认的建议(与 `--stale` 二选一) |
| `--stale` | 否 | — | 列出待确认的过期条目(与 `--pending` 二选一) |

### knowledge show

看一条条目或建议

```
tightrein knowledge show <id>
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | 建议编号(如 3)或条目编号(如 CON-0001) |

### knowledge confirm

确认一条建议并写成条目

```
tightrein knowledge confirm <id>
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | 建议编号，如 3 |

### knowledge drop

放弃一条建议

```
tightrein knowledge drop <id> [--note <note>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `<id>` | 是 | — | 建议编号，如 3 |
| `--note <note>` | 否 | — | 补充说明 |

### knowledge add

手写一条条目

```
tightrein knowledge add --kind {conventions,patterns,lessons} --slug <slug> --title <title> --summary <summary> --body <body> [--location <location>]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--kind {conventions,patterns,lessons}` | 是 | — | — |
| `--slug <slug>` | 是 | — | 文件名中的短名(小写字母、数字、连字符) |
| `--title <title>` | 是 | — | — |
| `--summary <summary>` | 是 | — | — |
| `--body <body>` | 是 | — | — |
| `--location <location>` | 否 | — | 涉及的位置(可写多次) |

## admin

本机的安装、自检与维护

### admin install

装到 agent 工具(先检查后执行)

```
tightrein admin install [--tool {claude,agy,codex}] [--dry-run]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--tool {claude,agy,codex}` | 否 | — | 只处理这个工具(可写多次) |
| `--dry-run` | 否 | — | 只列出要做什么，不执行 |

### admin uninstall

卸下(只删记录过的)

```
tightrein admin uninstall [--tool {claude,agy,codex}] [--dry-run]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--tool {claude,agy,codex}` | 否 | — | 只处理这个工具(可写多次) |
| `--dry-run` | 否 | — | 只列出要做什么，不执行 |

### admin rebuild

从文件重建数据库

```
tightrein admin rebuild
```

### admin check

自检

```
tightrein admin check
```

### admin clean

立即按保留期清理

```
tightrein admin clean [--dry-run]
```

| 参数 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `--dry-run` | 否 | — | 只列出要做什么，不执行 |
