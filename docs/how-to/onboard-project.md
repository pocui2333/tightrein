# 接入项目

把一个真实项目接进 tightrein。第一次用建议先走一遍[第一次运行](../tutorials/first-run.md)；接入步骤的设计依据见 `src/tightrein/onboard/README.md`。

## 步骤

| 步骤 | 命令 | 做什么 |
|---|---|---|
| 1 指定仓库 | `tightrein project add <仓库路径> [--name <名>]` | 建工作区 `workspaces/<名>/`，探测仓库，写 `setup.json`、`settings.json` 的草稿与 `setup.md` |
| 2 确认 | 编辑 `setup.json`、`settings.json`，填 `sites.json`、`secrets.json` | 每个模块定启用、不启用还是自定义；自定义的照方法文档写 `scripts/` 下的脚本 |
| 3 试跑 | `tightrein project check [<模块>]` | 每个启用与自定义的模块各试跑一次(只取数据，不调用模型)；只读 worktree 切到主分支，把检查命令全量跑一次 |
| 4 就绪 | `tightrein project ready` | 试跑全部通过且之后没改过 `setup.json` 才能标为就绪；macOS 上同时装上定时器 |

接入期只做只读的事：不采集入库、不改代码、不提 PR。探测不到的写 null，不猜，`setup.md` 里列为「待填」。

## 逐个模块定状态

`setup.json` 的 `modules` 下每个模块都要明确写出(漏写报错)，字段 `status`、`method`、`script`、`guide`、`reason`、`impact` 都要有，不适用的写 null：

| `status` | 必填 |
|---|---|
| `enabled` | 模块有可选方法时写 `method` |
| `disabled` | `reason`；`impact` 写少了什么能力、靠什么兜底(为 null 的列为盲区) |
| `custom` | `script`(相对工作区)、`guide`(照哪份方法文档写的)、`reason`；脚本要用凭据时加 `secrets`(secrets.json 中的条目名列表) |

| 模块 | 怎么选 | 要填的 | 详见 |
|---|---|---|---|
| `collect.project_probes` | 有只有项目自己知道的业务异常(定时任务没跑完、处理量为零)时 `enabled`，`method` 为 null | 在 `settings.json` 的 `overrides.controls."collect.project_probes".probes` 登记探针；脚本放 `scripts/` | [写项目探针](write-project-probe.md) |
| `collect.platform_errors` | 用 Sentry 或 Loki 时 `enabled`，`method` 为 `sentry`、`loki` 或 `sentry+loki` | sites：`sentry.url`、`sentry.organization`、`loki.url`(`loki.user`、`loki.tenant`)；secrets：`sentry.token`、`loki.token`；用 Loki 时 `controls."collect.platform_errors".logQuery` | `src/tightrein/collect/platform_errors/README.md` |
| `collect.access_log` | 访问日志在 Loki：`enabled`，`method: "loki"`；在项目自己的日志文件或数据库：`custom` | Loki：`controls."collect.access_log".query`；自定义：照方法文档写取数脚本 | `src/tightrein/collect/access_log/README.md`、`project_sources/` |
| `collect.alerts` | 有 Alertmanager(或 Grafana 告警)时 `enabled`，`method: "alertmanager"` | sites：`alertmanager.url`；secrets：`alertmanager.token`(可无) | `src/tightrein/collect/alerts/README.md` |
| `collect.api_fuzz` | 有 OpenAPI 或 Swagger 接口描述时 `enabled`，`method` 为 `openapi_file` 或 `openapi_url` | sites：`target.baseUrl`、`target.environment`；测生产环境时只能测 GET，必须给允许清单 | `src/tightrein/collect/api_fuzz/README.md` |
| `collect.static` | 一般 `enabled`(会调用模型，采集中最贵) | 规则集：`controls."collect.static".semgrep.configs`(如 `["p/python"]`) | `src/tightrein/collect/static/README.md` |
| `collect.incidental` | `enabled` | 无 | `src/tightrein/collect/incidental/README.md` |
| `implement.check.runtime` | 要在本机启动服务查改动涉及的接口与页面时 `custom` | `script`：项目的启动脚本(输出 `launch.schema.json` 格式的启动计划)；端口等在 `controls."implement.check.runtime"` | `src/tightrein/implement/check/runtime/README.md` |
| `release.deploy` | 有部署记录可读时 `enabled`，`method` 为 `github_actions`、`github_deployments` 或 `vercel`(方法库 `src/tightrein/release/deploy_source/`) | 参数在 `controls."release.deploy".<方法>`，按同名清单校验；Vercel 的令牌写在 secrets.json 的 `vercel.token`；不启用时合并后按 `assumeDeployedAfter` 视为已部署 | `src/tightrein/release/deploy_source/README.md` |
| `release.accept` | 要在部署后观察回归时 `enabled`(只能 enabled 或 disabled) | 各来源的观察期在 `controls."release.accept".windows`；不启用时合并并部署即完成，不观察 | `src/tightrein/release/README.md` |
| `release.github_issues` | 要把 Issue 镜像到 GitHub 时 `enabled`(只能 enabled 或 disabled) | 镜像只看这一项(origin 还要是 GitHub 仓库)；标签前缀在 `controls."assess.issue".github.labelPrefix` | `src/tightrein/assess/issue/README.md` |

每个方法要哪些参数，见 [平台方法](../reference/methods.md)。

## 项目级配置(settings.json)

两部分：

- `project`(项目事实)：`repo`(绝对路径)、`mainBranch`、`language`(`zh` 或 `en`，决定 Issue、PR、文档与命令行的输出语言)、`commands`(`test`、`lint`、`build`、`typecheck`，可加 `prepare` 作每个 worktree 的准备命令，如装依赖)、`testPatterns`、`frontendPatterns`；
- `overrides`：对全局配置的覆盖，键与 `settings/defaults.json` 相同，只写要改的键。

覆盖规则(实现在 `src/tightrein/settings/load.py`，详见 `settings/README.md`)：

- 读取顺序 `settings/defaults.json` → `settings/controls.json`(本机) → 工作区 `overrides`，后者覆盖前者；映射按键递归合并；
- 单个值与列表由上层整体替换；键名后加 `+` 表示追加，如 `"boundaries": {"uncounted+": ["generated/"]}`；
- 受保护文件 `boundaries.protected.forbidden`、`boundaries.protected.highRisk` 只能用 `forbidden+`、`highRisk+` 追加，缺省项删不掉；
- 写死在协议层、覆盖即报错的：必须人工的关卡、子进程环境变量白名单、凭据与脱敏规则、文件命名与交接格式。

```sh
tightrein project config                                   # 合并后的全部取值
tightrein project config controls.implement.code.turns      # 某个调用点的生效值(按继承)
tightrein project config boundaries.changeCap --explain     # 每一层给出的值
```

全部键与缺省值见 [配置字段](../reference/configuration.md)。

## 地址与密钥

- `sites.json`：按平台分组的地址与查询条件，不含密钥；工作区的在全局 `settings/sites.json` 之上按键合并，项目的优先；被测服务写在 `target`(`baseUrl`、`environment`)；
- `secrets.json`：平铺的条目，键名「平台.条目」，值一律是字符串；权限必须是 600，否则拒绝运行；读到的值登记进脱敏，不交给 agent 进程。项目探针与自定义脚本只拿得到登记过的条目(经环境变量 `TIGHTREIN_SECRET_<条目名>`)。

两个文件都不提交。

## 自定义脚本的协议

`custom` 模块与项目探针的脚本都按同一协议调用(`src/tightrein/protocol/scripts.py`)：

- `.py` 用 tightrein 自己的解释器运行，其余直接执行；工作目录是工作区；
- 标准输入一个 JSON 对象(各模块的方法文档写明字段；试跑时带 `"trial": true`)；
- 环境变量只给白名单，加上登记的凭据；
- 标准输出单个 JSON 对象；方法文档旁有 `<文档名>.schema.json` 或模块的 `output.schema.json` 时按它校验，列出每条错误的路径；
- 退出码非 0 但标准输出是合法响应时以响应为准；无法启动、超时、输出不合格为不通过，附标准错误的最后几行(已脱敏)。

## 试跑与就绪

```sh
tightrein project check                    # 全部
tightrein project check collect.alerts     # 只试跑一个模块
tightrein project show                     # 在终端看接入清单与试跑结果
tightrein project ready
```

- 试跑结果写进 `setup.md` 与数据库，带 `setup.json` 的哈希；之后改了 `setup.json` 要重新试跑，再 `ready` 一次；
- 静态巡检这类会调用模型的模块在试跑中跳过，第一次运行时再看；
- `ready` 在 macOS 上装用户级 LaunchAgent `local.tightrein.<项目>`：每 `schedule.tick` 醒来，只在 `schedule.window` 内运行，自动推进到 `schedule.advanceTo`；日志在工作区 `data/logs/`。

## 之后

- 看状态：`tightrein status`；有盲区(不启用且没有兜底)时最后一行列出；
- 改了配置：`tightrein admin check` 自检(配置、接入清单、凭据文件权限、工具版本、心跳、残留)；
- 移除：`tightrein project remove <名>` 卸下定时器、删除工作区，不动仓库。
