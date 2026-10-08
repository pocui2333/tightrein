# settings：全局取值

协议层(`src/tightrein/protocol/`)定义规则与缺省值的含义，这里只放取值。项目专属的一律放在工作区(`workspaces/<项目>/`)，这里只放全局的。读取与校验在 `src/tightrein/settings/load.py`。

## 放什么

| 文件 | 内容 | 提交 |
|---|---|---|
| `defaults.json` | 全局缺省与各模块的缺省取值 | 提交 |
| `controls.json` | 本机对各阶段、各模块控制字段的覆盖，结构与 `defaults.json` 相同，只写要改的键 | 不提交 |
| `sites.example.json` | 网站地址与纯配置的示例模板(假值) | 提交 |
| `sites.json` | 不属于某个项目的网站地址与纯配置，照 `sites.example.json` 写 | 不提交 |
| `secrets.json` | 不属于某个项目的秘钥，如 GitHub 令牌 | 不提交，权限必须是 600 |

各阶段、各模块的文件夹里不写任何取值：只放程序、README 与方法清单(`<方法>.yaml`，只声明接受哪些参数、适用条件与限制)。

## 每个文件的字段

### defaults.json(与 controls.json)

| 键 | 内容 |
|---|---|
| `models.<别名>` | 模型别名：`tool`(claude、agy、codex)、`model`、`effort`、`price`(每百万 token 的美元：`input`、`output`、`cacheRead`、`cacheWrite`，工具不报费用时估算用；没有时为 null) |
| `controls.<控制键>` | 控制字段，见下表；控制键写成「阶段.模块.小步骤」，`*` 为全局缺省(必须有)；同一处也可写该模块自己的参数(如 `collect.static.review` 的 `maxClaims`)，由模块按自己的 schema 校验 |
| `independence` | `[审查者, 生成者]` 对：两者解析出的实际模型必须不同，各条件变体逐一比较 |
| `limits` | 重试(`retry`)、临时错误的识别(`transientPatterns`)、熔断(`breaker`)、各类超时(`timeouts`)、锁(`lock`)、中断收尾宽限(`shutdownGrace`)，见 `protocol/limits.md`、`recovery.md` |
| `resources` | 并发(`concurrency`)、订阅额度余量(`quota`)、每个 Issue 的 token 上限(`issueTokens`)、缓存读取的计量权重(`cacheReadWeight`)，见 `protocol/resources.md` |
| `boundaries` | 只读命令(`readCommands`)、改动量硬上限(`changeCap`)、自动确认门槛(`autoApprove`)、不计入改动量的文件(`uncounted`)、受保护文件(`protected.forbidden`、`protected.highRisk`)、关卡(`gates`)，见 `protocol/boundaries.md` |
| `records.retention` | 保留期，见 `protocol/records.md` |
| `schedule` | 醒来间隔、时段、每个来源的间隔、自动推进到哪一步，见 `protocol/schedule.md` |
| `git` | 分支、提交、PR 的格式串与任务类型的对应，见 `protocol/git.md` |
| `tools.<工具>.path` | 外部工具的路径；null 表示从 PATH 找 |

控制字段(全局缺省在 `controls.*`)：

| 字段 | 类型 | 含义 |
|---|---|---|
| `model` | 别名 | 这个调用点用的模型 |
| `modelWhen` | `{条件: 别名}` | 带条件时改用的模型，如 `{"high_risk": "fable"}` |
| `fallback` | 别名或 null | 被拒绝或依赖熔断时的备用模型 |
| `timeout` | 时长 | 这一级的时限；下级不超过上级，各阶段不超过一次完整运行(`limits.timeouts.run`) |
| `idle` | 时长 | 流式输出多久没有动静即超时 |
| `turns` | 整数 | 一次调用的轮数上限 |
| `inputTokens`、`outputTokens` | 整数 | 单次调用的输入与输出上限 |
| `rounds` | 整数 | 交回修改或重出的轮数上限 |
| `access` | `read` 或 `write` | 读写权限 |
| `network` | 布尔 | 是否允许联网 |

所有时长写成数字加单位：`500ms`、`30s`、`20m`、`2h`、`90d`。

### sites.json

按平台分组的地址与纯配置(不含秘钥)，如 `github.host`、`sentry.url`、`loki.url`、`alertmanager.url`。工作区的 `sites.json` 在它之上按键合并，项目的优先。

### secrets.json

平铺的秘钥条目，键名写成「平台.条目」，值一律是字符串，如 `{"github.token": "…", "sentry.token": "…"}`(嵌套会被拒绝)。程序读取时检查权限，不是 600 就拒绝运行；读到的值登记进脱敏，永不交给 agent 进程，不写进提示、交接文件与日志(见 `protocol/security.md`)。

## 怎么覆盖

读取顺序：`defaults.json` → `controls.json` → 工作区 `settings.json` 的 `overrides`，后者覆盖前者。

- 映射按键递归合并：只写要改的键，其余沿用下层；
- 单个值与列表由上层整体替换；
- 键名后加 `+` 表示追加到下层列表后(去重)，如 `"boundaries": {"uncounted+": ["vendor/"]}`；
- 只许追加的列表：`boundaries.protected.forbidden`、`boundaries.protected.highRisk`，上层只能写 `forbidden+`、`highRisk+`，写原键(替换)即报错，缺省项删不掉；
- 控制字段按「小步骤 → 模块 → 阶段 → `*`」继承：`implement.design.frontend` 没写的取 `implement.design`，再取 `implement`，最后取 `*`；带条件时先查 `modelWhen` 再查 `model`。

读取即校验，一次列出全部问题，每条带完整键名：控制键是否合规、字段类型、时长写法、别名是否存在、审查者与生成者是否同一模型、自动确认门槛是否超过改动量上限、下级时限是否超过上级。取不到的键报 `MissingSetting`，不悄悄给缺省值。`tightrein project config --explain <键>` 列出每一层给出的值。

## 不能覆盖的

写死在协议层，配置改不了：

- 必须人工的关卡(禁改文件被改、高风险合并、超出改动量上限、需要拍板)：`protocol/boundaries.py`；
- 受保护文件只能追加，不能删缺省项；
- 子进程的环境变量白名单：`protocol/security.py`；
- 凭据与脱敏规则：`protocol/security.py`；
- 文件命名、编号、交接文档的格式：`protocol/naming.md`、`handoff.md`。
