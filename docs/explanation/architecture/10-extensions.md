# 能力层：extensions

本篇落实 00 篇 3.6「通用与专属的划分」：核心只定义扩展点与调用机制，技术栈与项目专属的知识由扩展提供。内容包括扩展点的通用机制、每个扩展点的输入输出、没有扩展时的核心默认行为、技术栈扩展 `extensions/stacks/aspnetcore/` 与示例项目的项目扩展 `workspaces/demo/extensions/` 的组织与实现要点，以及扩展的测试方式。编号、枚举、表与路径沿用 `01-foundation.md`；使用扩展的探针见 `04-probes.md`，本机启动见 `07-fix-verify-release.md` 第 11 章。

## 1. 总体

### 1.1 职责与边界

| 负责 | 不负责 |
|---|---|
| 按 `project.yaml` 的声明与技术栈清单解析每个扩展点由谁实现(1.4) | 扩展内部的解析与工具调用(由扩展自己完成) |
| 以子进程启动扩展，经标准输入输出交换 JSON，处理超时与错误(第 2 章) | 根据扩展输出产出信号、判断问题(探针与流水线模块) |
| 按 `contracts/schemas/extension/points/` 校验请求与响应 | 扩展所需外部工具的安装(由扩展声明，用户按提示安装) |
| 按 commit 缓存与 commit 绑定的扩展输出(2.7) | — |
| 记录每次调用的事件，保存标准错误输出 | — |

`extensions` 是能力层组件，依赖基础层的 `contracts`、`config`、`store`、`observability` 与同层的 `guards`(生成不含凭证的子进程环境)；被 `probes`、`verify` 与读取部署记录的 `collect`、`release`、编排层使用。

### 1.2 三层实现

| 层 | 位置 | 由谁维护 | 例子 |
|---|---|---|---|
| 项目扩展 | `workspaces/<项目>/extensions/` | 工作区 | 示例项目的角色能力矩阵解析、日志目录读取、前端路由提取 |
| 技术栈扩展 | 本工具仓库根目录的 `extensions/stacks/<技术栈>/` | 本工具 | `aspnetcore`：接口描述导出、端点权限声明读取、控制台日志解析 |
| 核心默认 | `core/tightrein/extensions/defaults.py` | 本工具 | 每个扩展点在没有扩展时的行为(第 4 章) |

一段逻辑先写在项目扩展中；第二个同技术栈的项目也需要它时，再提升为技术栈扩展(00 篇 3.6 规则 4)。

### 1.3 文件划分

```
core/tightrein/extensions/
  points.py          扩展点枚举、各扩展点的默认超时、是否按 commit 缓存、是否可扩展
  resolve.py         按「项目扩展、技术栈扩展、核心默认」解析实现；读取 stack.yaml
  invoke.py          子进程启动、请求写入、响应读取、超时终止、错误码归类
  cache.py           与 commit 绑定的输出缓存及其元数据
  defaults.py        各扩展点的核心默认实现
  client.py          供探针与 verify 调用的类型化接口：每个扩展点一个函数
```

`client.py` 对外提供的函数：

| 函数 | 扩展点 | 调用方 |
|---|---|---|
| `spec_export(repo, commit) -> SpecResult` | `spec-export` | `sources/api_fuzz`、`sources/common/routes.py` |
| `authz_endpoints(repo, commit) -> EndpointsResult` | `authz-endpoints` | `sources/api_fuzz/authz`、`pipeline/verify/steps/scope.py` |
| `authz_roles(repo, commit, roles) -> RolesResult` | `authz-roles` | `sources/api_fuzz/authz` |
| `error_tracking(since, until) -> PointResult` | `error-tracking` | `sources/platform_errors` |
| `log_platform(query, since, until, limit) -> PointResult` | `log-platform` | `sources/platform_errors`、`sources/access_log`、`tightrein project probe logs` |
| `log_parse(chunks, state) -> ParseResult` | `log-parse` | `sources/platform_errors`、`tightrein project probe logs` |
| `alert_source() -> PointResult` | `alert-source` | `sources/alerts` |
| `static_tools(repo, commit, scope, raw_dir) -> ToolsResult` | `static-tools` | `sources/static` |
| `page_routes(repo, commit) -> RoutesResult` | `page-routes` | `pipeline/verify/steps/scope.py` |
| `local_run(worktree, mode, ports) -> LocalRunPlan` | `local-run` | `pipeline/verify/steps/local_run.py` |
| `deploy_source(repo, commit) -> PointResult` | `deploy-source` | `pipeline/common/deploys.py`(`release track`、`collect/steps/target.py`、编排的部署检测、`verify staging`) |

每个函数返回的结果带 `implementation`(`project`、`stack`、`default`)，调用方据此在运行摘要中说明数据来自哪一层。
`configured(point)` 判断扩展点是否由技术栈或项目实现，`method(point)` 返回选用的方法编号(api-fuzz 据此判断接口描述是否
由框架导出)。

### 1.4 方法目录

凡是只能按项目决定的部分，外部都先提供一份「方法目录」：每个扩展点有哪些方法、每种方法的具体实现与参数。项目在 `project.yaml` 中只写选用哪种方法与参数；目录中的方法都不适用时，才在项目扩展中写自定义实现。

**方法的来源**

| 来源 | 位置 | 放什么 |
|---|---|---|
| 核心方法 | `core/tightrein/extensions/methods/<扩展点>/<方法>.py` | 与技术栈无关的方法，例如平台的只读查询、按正则或 JSON Lines 解析日志、读取现成的 OpenAPI 文件或地址、手工列出的清单 |
| 技术栈方法 | `extensions/stacks/<技术栈>/<扩展点>/<方法>/` | 某一技术栈通用的方法，例如 Swashbuckle 导出接口描述、解析 .NET 控制台日志、静态解析 Vue Router 路由、npm audit |
| 项目自定义 | `workspaces/<项目>/extensions/` | 目录中没有适用方法时的项目专属实现 |

**方法的描述**：每种方法有一份清单，写明编号(`<来源>/<方法名>`，例如 `core/sentry`、`aspnetcore/dotnet-console`)、所属扩展点、一句话说明、适用条件、参数 schema(`optionsSchema`)、依赖的外部工具，以及方法参考的其余部分(`doc`：前提、输出、限制、示例配置)。`tightrein admin ext methods [<扩展点>]` 列出可选的方法及其说明；`docs/reference/methods.md` 由 `core/dev/contract_reference.py methods` 从清单生成，按 00-documentation.md 的方法参考模板列出全部核心方法与后续平台。

**各扩展点的方法目录**

| 扩展点 | 核心方法 | 技术栈方法 |
|---|---|---|
| `spec-export` | `core/openapi-file`(读取接口描述文件；`base: repo`(缺省)时相对仓库根目录、随 commit 取文件，`base: workspace` 时相对工作区根目录，供仓库中没有接口描述的项目使用)、`core/openapi-url`(从本机启动的服务读取接口描述) | `aspnetcore/swashbuckle-cli` |
| `authz-endpoints` | `core/openapi-security`(从接口描述的 security 声明推出端点所需权限)、`core/manual-list`(手工维护的端点权限清单) | `aspnetcore/endpoint-policies` |
| `authz-roles` | `core/manual-matrix`(在工作区中手工维护的角色能力矩阵 YAML) | — |
| `error-tracking` | `core/sentry`(Sentry 与兼容其 API 的 GlitchTip：窗口内有新事件的错误分组、最新事件的堆栈与操作轨迹) | — |
| `log-platform` | `core/loki`(Grafana Loki 的 `query_range`，按 LogQL 取窗口内的原文) | — |
| `alert-source` | `core/alertmanager`(Alertmanager API v2：Prometheus、Grafana 内置告警、Grafana Cloud、Mimir) | — |
| `log-parse` | `core/json-lines`、`core/regex`(按正则抽取时间、级别、类别与消息) | `aspnetcore/dotnet-console`(.NET simple 控制台格式，参数含本项目命名空间) |
| `static-tools` | `core/semgrep` | `aspnetcore/dotnet-build-warnings`、`aspnetcore/dotnet-vulnerable`、`node/npm-audit`、`node/npm-build-warnings` |
| `page-routes` | `core/manual-list` | `vue/vue-router-static`、`react/react-router-static` |
| `local-run` | `core/command-sequence`(按顺序启动一组命令，逐个等待就绪信号) | `aspnetcore/dotnet-run`、`node/npm-script` |
| `deploy-source` | `core/github-actions`(部署工作流的最近运行，`gh run list --workflow`)、`core/github-deployments`(GitHub Deployments 与各部署最新的状态)、`core/vercel`(Vercel REST API `v6/deployments`；令牌在方法进程内从钥匙串条目 `keychainItem` 读取，只用于请求头) | — |

目录随接入的项目逐步补充：接入新项目时如果需要写自定义实现，先判断它是否对同技术栈的其他项目也适用，适用的写成技术栈方法再选用。

### 1.5 查找顺序

`resolve.py` 对每个扩展点按下表依次查找，取第一个命中的实现：

| 顺序 | 条件 | 实现 |
|---|---|---|
| 1 | `extensions.<扩展点>.enabled` 为 `false` | 核心默认；用于关闭某个扩展点 |
| 2 | `extensions.<扩展点>.command` 存在 | 项目自定义；`mode` 为 `extend` 时先按第 3 到 5 行求出下一层，把其输出作为 `base` 传入(2.3) |
| 3 | `extensions.<扩展点>.use` 存在 | 方法目录中的该方法；方法所属的技术栈必须在 `stacks` 中声明 |
| 4 | `stacks` 中声明的技术栈在 `stack.yaml` 中为该扩展点指定了默认方法 | 该默认方法；多个技术栈都指定时启动报错，要求用 `use` 明确选择 |
| 5 | 以上都不满足 | 核心默认 |

- `mode: extend` 而下一层没有实现时，启动时报错并给出完整键名，不静默降级为 `replace`。
- 解析结果在启动时计算一次，`tightrein admin ext list` 列出每个扩展点的实现来源、方法编号、命令与生效的 `options`。

### 1.6 在 project.yaml 中的声明

```yaml
stacks: [aspnetcore, vue, node]      # 用到的技术栈；可省略
extensions:
  <扩展点>:
    use: <方法编号>                   # 从方法目录中选用；与 command 二选一
    command: [<可执行文件>, <参数>...] # 项目自定义实现
    mode: replace                    # replace(默认)或 extend，只用于 command
    options: { ... }                 # 方法或自定义实现的参数
    timeoutSeconds: 120
    enabled: true
```

| 键 | 类型 | 说明 |
|---|---|---|
| `stacks` | 字符串数组 | 用到的技术栈；每个都必须存在 `extensions/stacks/<技术栈>/stack.yaml` |
| `extensions.<扩展点>.use` | 字符串 | 方法编号；方法不存在、不属于该扩展点或所属技术栈未声明时启动报错 |
| `extensions.<扩展点>.command` | 字符串数组 | 相对路径以 `workspaces/<项目>/extensions/` 为基准解析；`{python}` 替换为核心虚拟环境的 Python 解释器路径；与 `use` 同时出现时报错 |
| `extensions.<扩展点>.mode` | `replace`、`extend` | `extend` 只允许用于第 3 章中标明可扩展的扩展点(`log-parse`、`static-tools`、`local-run`) |
| `extensions.<扩展点>.options` | 对象 | 选用方法时按该方法的 `optionsSchema` 校验；自定义实现时原样传入 |
| `extensions.<扩展点>.timeoutSeconds` | 整数 | 1 以上 |
| `extensions.<扩展点>.enabled` | 布尔 | 缺省为 `true` |

扩展点名只能取第 3 章的九个，其他键在校验时报错。

**示例项目中的取值**

```yaml
stacks: [aspnetcore, vue, node]
extensions:
  spec-export:
    use: aspnetcore/swashbuckle-cli
    options: { project: src/Demo.Api/Demo.Api.csproj, swaggerDoc: V1, fakeSecretEnv: [Audience__Secret] }
  authz-endpoints:
    use: aspnetcore/endpoint-policies
    options: { project: src/Demo.Api/Demo.Api.csproj }
  authz-roles:
    command: ["{python}", authz_roles.py]          # PermissionMatrix.cs 的写法是该项目独有的，没有通用方法
    options: { matrixFile: src/Auth/PermissionMatrix.cs }
  log-platform:
    use: core/loki
    options: { url: "https://loki.example.com", keychainItem: tightrein.demo.loki }
  log-parse:
    use: aspnetcore/dotnet-console
    options: { timezone: Asia/Shanghai, timestampFormat: "HH:mm:ss", rolloverToleranceMinutes: 30, projectNamespaces: [Demo] }
  static-tools:
    use: aspnetcore/dotnet-build-warnings
    options: { solution: src/Demo.sln }
  page-routes:
    use: vue/vue-router-static
    options: { routerFile: src/vue/src/router/index.js, frontendDir: src/vue }
  local-run:
    use: core/command-sequence
    options:
      services:
        - { name: backend, use: aspnetcore/dotnet-run, project: src/Demo.Api/Demo.Api.csproj, env: { ASPNETCORE_ENVIRONMENT: Development } }
        - { name: frontend, use: node/npm-script, dir: src/vue, script: serve }
  deploy-source:
    use: core/github-actions
    options: { workflow: deploy.yml }
```

示例项目只有 `authz-roles` 一处需要自定义实现，其余都从方法目录中选用。

## 2. 调用机制

### 2.1 子进程

| 项 | 做法 |
|---|---|
| 启动 | `invoke.py` 以参数数组启动，不经过 shell；工作目录为扩展所在目录(项目扩展为 `workspaces/<项目>/extensions/`，技术栈扩展为 `extensions/stacks/<技术栈>/`) |
| 进程组 | 在独立的进程组中启动，超时或中断时终止整个进程组 |
| 环境变量 | 由 `guards.credentials.build_env` 生成，不含任何凭证；另加 2.5 的变量 |
| 标准输入 | 写入一个请求 JSON(2.2)后关闭 |
| 标准输出 | 只能输出一个响应 JSON(2.3)；上限 64 MB，超出按 `protocol-error` 处理；大体积结果写入请求给出的 `scratchDir` 并以路径引用 |
| 标准错误 | 扩展的日志，经 `observability/redact.py` 脱敏后保存到 `data/runs/<运行编号>/raw/extensions/<扩展点>[-<序号>].stderr.log`，只保留最后 1 MB |
| 并发 | 同一扩展点在同一运行内串行调用；不同扩展点可以由不同探针并行调用 |

扩展不导入 `tightrein` 包，只依赖自身语言的标准库与所声明的外部工具，以保证可以脱离核心单独运行与测试。

### 2.2 请求

所有扩展点共用同一个外层，`input` 为各扩展点特有的字段(第 3 章)：

| 字段 | 类型 | 说明 |
|---|---|---|
| `protocol` | 整数 | 协议版本，当前为 1；扩展遇到不支持的版本时以 `invalid-input` 结束 |
| `point` | 字符串 | 扩展点名 |
| `workspace` | 字符串 | 工作区绝对路径 |
| `repo` | 字符串或空 | 被测项目代码的绝对路径：只读 worktree 或修复 worktree；`error-tracking`、`log-platform`、`log-parse`、`alert-source` 为空；`spec-export` 的实现不读仓库时(`core/openapi-file` 的 `base: workspace`)为空 |
| `commit` | 字符串或空 | `repo` 当前的 commit；`error-tracking`、`log-platform`、`log-parse`、`alert-source` 为空 |
| `options` | 对象 | `extensions.<扩展点>.options`，技术栈扩展时已与 `stack.yaml` 的默认值合并 |
| `scratchDir` | 字符串 | 本次调用可写的临时目录，调用结束后由核心删除；需要保留的文件由扩展写入 `input` 中给出的输出路径 |
| `base` | 对象或空 | 仅 `extend` 模式：下一层实现的 `output`，已通过校验 |
| `input` | 对象 | 扩展点特有的输入 |

### 2.3 响应

| 字段 | 类型 | 说明 |
|---|---|---|
| `protocol` | 整数 | 与请求一致 |
| `status` | `ok`、`error` | 结果 |
| `output` | 对象 | `status` 为 `ok` 时必填，结构见第 3 章 |
| `error` | 对象 | `status` 为 `error` 时必填：`code`(2.4)、`message`、`hint`(建议的处理方式，可空) |
| `notes` | 字符串数组 | 需要写入运行摘要的说明，例如「3 个端点无法解析路由」 |

`extend` 模式下项目扩展返回的是最终结果：它可以原样保留、修改或补充 `base` 中的内容，核心不做合并，只校验最终的 `output`。

### 2.4 错误码

| 错误码 | 产生方 | 含义 | 调用方的处理 |
|---|---|---|---|
| `invalid-input` | 扩展 | 请求或 `options` 不合法 | 按失败处理，提示检查 `extensions.<扩展点>.options` |
| `not-applicable` | 扩展 | 当前代码中没有该扩展要读取的内容，例如仓库中找不到路由文件 | 视为该扩展点无结果，按核心默认处理，原因写入 `notes` |
| `tool-missing` | 扩展 | 所需外部工具未安装或版本不符 | 按失败处理，`hint` 给出安装命令 |
| `build-failed` | 扩展 | 构建失败 | 按失败处理，构建输出路径写入 `hint` |
| `source-unavailable` | 扩展 | 数据来源不可访问，例如日志目录未挂载 | 按失败处理，不更新读取位置 |
| `parse-failed` | 扩展 | 读到了内容但无法解析 | 按失败处理 |
| `timeout` | 核心 | 超过超时时间，进程组已终止 | 按失败处理 |
| `crashed` | 核心 | 退出码非 0 且标准输出没有合法的响应 | 按失败处理，附标准错误的最后 50 行 |
| `protocol-error` | 核心 | 标准输出不是单个 JSON、超出上限，或 `protocol` 不一致 | 按失败处理 |
| `schema-invalid` | 核心 | 响应不符合该扩展点的输出 schema | 按失败处理，附每条错误的 JSON 路径与原因 |

- 扩展以 `status: error` 正常返回时退出码为 0；退出码非 0 而标准输出含合法的错误响应时，以响应为准。
- `extend` 模式下下一层失败时不调用项目扩展，直接以下一层的错误返回。
- 各扩展点失败时对探针与验证的影响写在第 3 章各节的「失败时」一行。

### 2.5 环境变量

| 变量 | 含义 |
|---|---|
| `TIGHTREIN_EXTENSION_POINT` | 扩展点名 |
| `TIGHTREIN_EXTENSION_PROTOCOL` | 协议版本 |
| `TIGHTREIN_CACHE_DIR` | 该扩展可长期使用的缓存目录：`~/.cache/tightrein/extensions/<技术栈或工作区名>/`，例如 `EndpointPolicies` 的构建产物 |
| `DOTNET_CLI_TELEMETRY_OPTOUT`、`NO_UPDATE_NOTIFIER` 等 | 由 `stack.yaml` 的 `env` 声明的非敏感变量 |

### 2.6 超时

| 扩展点 | 默认超时(秒) | 说明 |
|---|---|---|
| `spec-export` | 900 | 含构建 |
| `authz-endpoints` | 900 | 含构建；与 `spec-export` 共用构建产物时通常很短 |
| `authz-roles` | 120 | — |
| `error-tracking`、`log-platform` | 120 | — |
| `alert-source` | 60 | — |
| `log-parse` | 120 | — |
| `static-tools` | 1800 | 含构建与依赖漏洞查询 |
| `page-routes` | 120 | — |
| `local-run` | 30 | 只返回启动计划，不启动服务；服务的就绪超时由 `localRun.readyTimeoutSeconds` 控制 |
| `deploy-source` | 120 | 方法内每条请求另有 `timeoutSeconds` |

`extend` 模式下两层各自计时，超时值相同。

### 2.7 按 commit 缓存

`spec-export`、`authz-endpoints`、`authz-roles`、`page-routes` 的输出只取决于代码，按 commit 缓存在 `data/specs/<commit>/`：

| 文件 | 内容 |
|---|---|
| `openapi.json` | `spec-export` 写出的接口描述 |
| `spec-export.json`、`authz-endpoints.json`、`authz-roles.json`、`page-routes.json` | 各扩展点的 `output` |
| `<扩展点>.meta.json` | 缓存键：实现层、命令、生效的 `options` 的哈希、技术栈扩展的 `version`、项目扩展目录中该命令文件的哈希、生成时间 |
| `authz-model.json` | 由 `sources/api_fuzz/authz/model.py` 合并两份权限数据得到 |

- 读缓存时比对 `meta.json` 中的缓存键，不一致则重新调用。
- 缓存缺失而 `repo` 的 HEAD 不等于所需 commit 时不调用扩展，调用方按「只读 worktree 不在目标 commit」处理。实现不读仓库时(`ExtensionClient.needs_repo` 为假，目前只有 `core/openapi-file` 的 `base: workspace`)不检查 HEAD、不要求只读 worktree，`repo` 为空，结果仍按 commit 缓存。
- `error-tracking`、`log-platform`、`log-parse`、`alert-source`、`static-tools`、`local-run`、`deploy-source` 不缓存。
- `core/openapi-file` 以 `base: workspace` 读取工作区中的文件时，缓存键不含该文件的内容(与 `core/manual-list`、`core/manual-matrix` 读取工作区文件时相同)；修改文件后删除对应 commit 的 `data/specs/<commit>/spec-export.*` 即可重新读取。

### 2.8 事件与脱敏

- 每次调用写一个 `operation` 为 `run_script` 的 span，`attributes` 含 `point`、`implementation`、`command`、`exitCode`、`status`、`errorCode`、`durationMs`、`cached`。
- 请求与响应不写入事件日志；`--output` 模式下请求与响应的副本写入 `<目录>/extensions/`，供对比与制作夹具。
- 平台返回的内容在进入信号前由 `sources/common/redact.py` 脱敏(04 篇 1.5)；平台的只读令牌只在方法进程内从钥匙串读取。

## 3. 扩展点

各扩展点的 schema 为 `contracts/schemas/extension/points/<扩展点>.input.schema.json` 与 `<扩展点>.output.schema.json`，外层为 `extension/extension-request.schema.json` 与 `extension/extension-response.schema.json`。下文的 `input` 与 `output` 即请求与响应中的同名字段。路径类字段除注明外均为相对 `repo` 的路径，使用 `/` 分隔。

### 3.1 spec-export

| 项 | 内容 |
|---|---|
| 用途 | 从代码导出 OpenAPI 接口描述，供 api-fuzz 调用 Schemathesis、verify 把实际路径匹配为路由模板 |
| 缓存 | 按 commit |
| 可扩展 | 否 |

| `input` 字段 | 说明 |
|---|---|
| `outputFile` | 接口描述的写入路径(绝对路径)，即 `data/specs/<commit>/openapi.json` |

| `output` 字段 | 说明 |
|---|---|
| `specFile` | 实际写入的路径，必须等于 `outputFile` |
| `format` | `openapi-3.0`、`openapi-3.1`、`swagger-2.0` |
| `operationCount` | 接口描述中的操作数 |
| `tool` | `name`、`version`：实际使用的导出工具 |
| `logFile` | 构建与导出输出在 `scratchDir` 之外的保存位置，可空；核心复制到 `raw/api-fuzz/spec-export.log` |

核心在收到响应后再检查 `specFile` 是可解析的 JSON 且含 `paths`，否则按 `schema-invalid` 处理。

| 失败时 | api-fuzz 为 `failed` |
|---|---|

### 3.2 authz-endpoints

| 项 | 内容 |
|---|---|
| 用途 | 列出每个端点所需的权限，供 api-fuzz 的越权检查；带源文件时供 verify 从改动文件推出受影响的接口 |
| 缓存 | 按 commit |
| 可扩展 | 否 |

`input` 为空对象。

| `output` 字段 | 说明 |
|---|---|
| `endpoints[].method` | HTTP 方法，大写 |
| `endpoints[].route` | 路由模板，写法与 `spec-export` 输出的 `paths` 键一致，例如 `/api/Order/{id}` |
| `endpoints[].requires` | 所需能力名的列表；多个能力同时需要 |
| `endpoints[].anonymous` | 是否允许匿名访问；为 `true` 时 `requires` 被忽略 |
| `endpoints[].symbol` | 处理该端点的 `类名.方法名` |
| `endpoints[].sourceFile` | 处理方法所在的源文件，取不到时为空 |
| `unresolved[]` | 无法得出路由或权限的端点：`symbol`、`reason` |

| 失败时 | 越权检查不运行，`notes` 说明原因；其余检查照常；verify 只用 `fix` 交接文档中的 `affectedEndpoints` |
|---|---|

### 3.3 authz-roles

| 项 | 内容 |
|---|---|
| 用途 | 给出每个测试角色具备哪些能力，与 `authz-endpoints` 合成越权检查的数据 |
| 缓存 | 按 commit |
| 可扩展 | 否 |

| `input` 字段 | 说明 |
|---|---|
| `roles` | `project.yaml` 中 `accounts.roles` 的角色键列表(不含匿名身份 `anonymous`) |

| `output` 字段 | 说明 |
|---|---|
| `capabilities` | 项目中定义的全部能力名 |
| `roles` | 对象：角色键 → { 能力名 → 是否具备 }；须包含 `input.roles` 中的每个角色 |
| `sourceFiles` | 读取的源文件 |

- 能力名须与 `authz-endpoints` 的 `requires` 使用同一套名称；合并时出现在 `requires` 中而不在 `capabilities` 中的能力列入 `notes`，涉及的端点不参与越权检查。
- 缺少某个角色时该角色不做越权检查，列入 `notes`。

| 失败时 | 与 `authz-endpoints` 相同 |
|---|---|

### 3.4 error-tracking、log-platform、alert-source

三个平台读取的扩展点都只读、不缓存、不可扩展，请求的 `repo`、`commit` 为空；令牌在方法进程内从 `options.keychainItem`
指定的钥匙串条目读取，只放进请求头。读取位置(时间窗口的终点)由核心保存(`source_cursors`，04 篇 4.1)。

| 扩展点 | `input` | `output` | 失败时 |
|---|---|---|---|
| `error-tracking` | `since`、`until`(UTC 窗口) | `issues[]`：`group`(平台分组编号，写成 `<平台>:<编号>`，作为问题指纹)、`kind`(backend、frontend)、`title`、`type`、`message`、`culprit`、`level`、`count`、`userCount`、`firstSeen`、`lastSeen`、`release`、`environment`、`permalink`、`frames[]`(file、function、line、inApp，出错处在前)、`breadcrumbs[]`、`url`、`browser`；`truncated`；`oldestAvailable`(平台还能查到的最早时间) | 内部错误的该来源不产出信号、不更新读取位置，另一个来源照常 |
| `log-platform` | `query`(平台的查询语句)、`since`、`until`、`limit` | `chunks[]`(`stream`、`text`、`startPosition`、`endPosition`、`modifiedAt`，与 `log-parse` 的输入相同)、`truncated`、`oldestAvailable` | 同上；访问日志为 `failed` |
| `alert-source` | 无 | `alerts[]`：`fingerprint`、`name`、`labels`、`annotations`、`startsAt`、`generatorUrl` | 业务告警为 `failed` |

### 3.5 log-parse

| 项 | 内容 |
|---|---|
| 用途 | 把日志原文切分为结构化条目：时间、级别、类别、异常、堆栈帧，并标出属于本项目的帧 |
| 缓存 | 不缓存；解析状态由核心与读取位置一起保存 |
| 可扩展 | 是：项目扩展可以在技术栈扩展的结果上改写 `isProject` 或补充字段 |

| `input` 字段 | 说明 |
|---|---|
| `chunks` | `log-platform` 输出的 `chunks`，原样传入 |
| `state` | 上次返回的 `state`，首次为空；核心不解读其内容，例如补齐日期所需的最后一条本地时间 |

| `output` 字段 | 说明 |
|---|---|
| `entries[].stream`、`entries[].position` | 条目所在的流与起始位置 |
| `entries[].occurredAt` | UTC 时间，已补齐日期并换算时区 |
| `entries[].localTime` | 原文中的时间，可空 |
| `entries[].level` | 归一化级别：`trace`、`debug`、`information`、`warning`、`error`、`critical` |
| `entries[].rawLevel` | 原文中的级别写法 |
| `entries[].category`、`entries[].eventId` | 日志类别与事件编号，可空；事件编号为整数(全为数字的字符串按整数)或原样保留的非空字符串(例如运行编号) |
| `entries[].message` | 日志消息 |
| `entries[].exception` | 可空：`type`、`message` |
| `entries[].frames[]` | 堆栈帧：`symbol`(`命名空间.类名.方法名`)、`file`(可空)、`line`(可空)、`isProject`(是否属于本项目) |
| `entries[].raw` | 条目原文 |
| `state` | 新的解析状态 |
| `unparsed` | 无法归入任何条目的行数；写入 `stats` |

内部错误只保留 `level` 属于 `sources.platform-errors.levels`(默认 `error`、`critical`)的条目；`frames` 中 `isProject` 为 `true` 的帧按顺序取前 10 个，作为信号的 `context.projectFrames`(04 篇 4.1)。

| 失败时 | 内部错误的日志来源不产出信号、不更新读取位置 |
|---|---|

### 3.6 static-tools

| 项 | 内容 |
|---|---|
| 用途 | 运行与技术栈有关的确定性检查工具(构建告警、依赖漏洞等)，把结果转为统一的发现条目 |
| 缓存 | 不缓存 |
| 可扩展 | 是：项目扩展在 `base` 的基础上追加自己的工具结果 |

| `input` 字段 | 说明 |
|---|---|
| `level` | `incremental` 或 `full`；static 探针的 `baseline` 档位与第一次巡检都按 `full` 请求 |
| `baseCommit` | 增量的起点，`full` 时可空 |
| `changedFiles` | `baseCommit` 到 `commit` 的改动文件；扩展可据此跳过与改动无关的工具 |
| `rawDir` | 工具输出的保存目录(绝对路径)，即 `data/runs/<运行编号>/raw/static/` |

| `output` 字段 | 说明 |
|---|---|
| `tools[].name` | 工具名，同时作为 `ToolFinding.tool` |
| `tools[].status` | `ok`、`failed`、`skipped`(与改动无关而未运行) |
| `tools[].exitCode` | 退出码，未运行时为空 |
| `tools[].logFile` | 相对 `rawDir` 的输出文件 |
| `tools[].reason` | `failed`、`skipped` 时的原因 |
| `findings[].tool` | 产生该条目的工具名 |
| `findings[].kind` | `build-warning`、`vulnerability`、`lint` |
| `findings[].rule` | 告警代码、漏洞编号或规则名 |
| `findings[].file`、`findings[].line`、`findings[].column` | 位置；依赖漏洞以依赖清单文件为 `file`，`line` 为空 |
| `findings[].message` | 原文消息 |
| `findings[].severity` | `info`、`low`、`medium`、`high`、`critical` |
| `findings[].package` | 仅依赖漏洞：`name`、`version`、`advisoryUrl` |

核心把 `findings` 转为 `ToolFinding`，`incremental` 档位只保留 `file` 在 `changedFiles` 中的条目(依赖漏洞的依赖清单文件不在改动中时同样保留，漏洞与改动无关也需要报告)。某个工具 `failed` 时探针为 `partial`(04 篇 5.6)。

| 失败时 | 确定性工具的结果为空，Semgrep 与审查照常，探针为 `partial` |
|---|---|

### 3.7 page-routes

| 项 | 内容 |
|---|---|
| 用途 | 列出前端的页面路由，供 verify 从改动的页面文件推出受影响的页面 |
| 缓存 | 按 commit |
| 可扩展 | 否 |

`input` 为空对象。

| `output` 字段 | 说明 |
|---|---|
| `routes[].path` | 完整的页面路由模板，嵌套路由已拼接，例如 `/material/detail/:id` |
| `routes[].name` | 路由名，可空 |
| `routes[].componentFile` | 页面组件的源文件，取不到时为空 |
| `routes[].meta` | 路由的附加信息，原样透传，可空 |
| `sourceFiles` | 读取的路由文件 |

只列出可以打开的页面：纯重定向与兜底路由(例如 `*`)不列入。

| 失败时 | 与没有扩展相同(4.1) |
|---|---|

### 3.8 local-run

| 项 | 内容 |
|---|---|
| 用途 | 给出在本机从 worktree 启动被测服务的计划：命令、环境、端口、就绪与失败信号；核心据此启动、判断就绪与收尾 |
| 缓存 | 不缓存 |
| 可扩展 | 是：项目扩展在 `base` 的基础上增加服务或修改服务参数 |

| `input` 字段 | 说明 |
|---|---|
| `mode` | `api`(只启动后端)或 `page`(后端与前端) |
| `ports` | `backend`、`frontend`(`api` 模式为空)，取自 `localRun.ports`(07 篇 11.2) |

| `output` 字段 | 说明 |
|---|---|
| `services[].name` | 服务名，复现检查 `check.yaml` 的 `requires` 使用同一名称 |
| `services[].argv` | 启动命令的参数数组 |
| `services[].cwd` | 工作目录，相对 `repo` |
| `services[].env` | 追加的环境变量，只能是非敏感值；核心校验值中不含凭证模式 |
| `services[].port` | 监听端口 |
| `services[].readyUrl` | 就绪后用于确认端口可达的 HTTP 地址 |
| `services[].readyPatterns` | 日志中表示启动成功的正则 |
| `services[].failPatterns` | 日志中表示启动失败的正则 |
| `services[].after` | 须先就绪的服务名，可空 |
| `unavailable[]` | 不在本机启动的服务：`name`、`reason`；`requires` 含这些服务的检查记为「未验证」 |
| `migrationPaths` | 迁移文件的路径模式；改动命中时启动前需要确认(07 篇 11.3) |

扩展只描述计划，不启动任何进程；启动、日志采集、就绪判断、收尾全部由核心执行(07 篇 11.4、11.5)。`page` 模式的计划中必须有名为 `frontend` 的服务，否则页面类检查记为「未验证」，原因「启动计划中没有前端服务」。

| 失败时 | 与没有扩展相同(4.1) |
|---|---|

### 3.9 deploy-source

| 项 | 内容 |
|---|---|
| 用途 | 以只读方式读取部署平台上最近的部署记录；包含某个合并提交的部署、观察期由核心判断(`pipeline/common/deploys.py`，07 篇 19.7) |
| 缓存 | 不缓存 |
| 可扩展 | 否 |

| `input` 字段 | 说明 |
|---|---|
| `branch` | 部署所跟随的分支，取 `project.mainBranch` |

| `output` 字段 | 说明 |
|---|---|
| `deployments[].id` | 平台上的部署编号 |
| `deployments[].commit` | 部署的 commit |
| `deployments[].status` | `running`、`succeeded`、`failed`、`skipped` |
| `deployments[].environment`、`url` | 环境与链接，可空 |
| `deployments[].createdAt` | 创建时间(UTC)；记录按创建时间从早到晚排列 |

凭证不经环境变量传入(扩展进程的环境会滤掉凭证)：需要令牌的方法在方法进程内从钥匙串读取，只用于请求，不写入输出与日志。

| 失败时 | 本次跟踪跳过，运行摘要中说明；下次运行重试 |
|---|---|

## 4. 核心默认实现

### 4.1 没有扩展时的行为

| 扩展点 | 核心默认 | 对下游的影响 |
|---|---|---|
| `spec-export` | 没有接口描述 | api-fuzz 返回 `skipped`，原因「未提供 spec-export 扩展」；接口覆盖率记为未知(`endpointsTotal` 为空) |
| `authz-endpoints` | 没有端点权限数据 | 不注册越权检查；`status_code_conformance` 的 401、403 失败无法判断是否为预期的拒绝，不产出信号，计入 `stats.unjudgedAuthFailures` |
| `authz-roles` | 没有角色能力数据 | 同上 |
| `error-tracking`、`log-platform`、`alert-source` | 没有接入平台 | 对应的采集方法为 `skipped`(未启用)，运行摘要列出；运行时缺陷由 api-fuzz 与静态巡检发现 |
| `log-parse` | 没有解析器 | `sources.platform-errors.logQuery` 有值而没有 `log-parse` 时启动报错(4.2)；`tightrein project probe logs` 返回原文行 |
| `static-tools` | 没有确定性工具 | 静态巡检只运行 Semgrep 与审查，运行摘要注明「未配置确定性工具」 |
| `page-routes` | 没有页面路由清单 | `coverage.pagesTotal` 为空，页面覆盖率记为未知；`coverage.pages` 记录实际页面路径；verify 只用 `fix` 交接文档中的 `affectedPages` |
| `local-run` | 没有启动计划 | 需要本机服务的 `api`、`page` 类检查一律记为「未验证」，原因「未提供 local-run 扩展」；`static` 类检查照常 |
| `deploy-source` | 没有部署记录 | 合并时间加 `release.deploy.observationHours`(缺省 24)视为已部署，部署记录的来源记为 `merge-time`；采集的目标版本为空，在运行说明中写明 |

### 4.2 启动时的检查

`config` 读取 `project.yaml` 后，`resolve.py` 执行：

1. `stack` 指向的目录与 `stack.yaml` 存在且合格。
2. `extensions` 下的键都是已知扩展点；`mode: extend` 的扩展点允许扩展且下一层有实现。
3. 项目扩展的命令文件存在；技术栈扩展的 `options` 符合其 `optionsSchema`。
4. `sources.platform-errors.logQuery` 有值(内部错误要读日志平台)时 `log-parse` 必须有实现，字段映射由它完成。

任一项不满足即报出完整键名并退出。外部工具(例如 .NET SDK)是否安装不在启动时检查，由扩展在调用时以 `tool-missing` 报告。

## 5. 技术栈扩展：aspnetcore

### 5.1 目录

```
extensions/stacks/aspnetcore/
  stack.yaml                  技术栈清单(5.2)
  common/
    build.py                  按 options.project 执行 dotnet build；同一 commit 的构建产物在 spec-export、authz-endpoints、static-tools 之间复用
    dotnet.py                 dotnet 与全局工具的定位、版本读取
    msbuild.py                MSBuild 告警与错误行的解析
    csproj.py                 读取项目文件中的 PackageReference 与 TargetFramework
  spec_export.py              spec-export
  authz_endpoints/
    run.py                    首次使用时构建 EndpointPolicies 到缓存目录，调用并转换输出
    EndpointPolicies/         C# 控制台工具：只读加载程序集，读出端点的路由与权限声明
  log_parse.py                log-parse：simple 控制台格式
  static_tools.py             static-tools：构建告警、依赖漏洞
  local_run.py                local-run：dotnet run
  schemas/
    <扩展点>.options.schema.json   各扩展点 options 的 schema
  tests/
    fixtures/<扩展点>/<用例>/  input.json、expected.json、files/(源文件、日志与工具输出的样本)
    sample-app/               测试用的小型 ASP.NET Core 项目：几个控制器，含 Policy、匿名与路由前缀
    test_<扩展点>.py
```

### 5.2 stack.yaml

| 键 | 说明 |
|---|---|
| `name` | `aspnetcore` |
| `version` | 扩展版本，进入缓存键 |
| `requires` | 外部工具与最低版本：.NET SDK 8 及以上 |
| `env` | 调用时追加的非敏感变量：`DOTNET_CLI_TELEMETRY_OPTOUT=1`、`DOTNET_NOLOGO=1` |
| `points.<扩展点>.command` | 命令，例如 `["{python}", "spec_export.py"]` |
| `points.<扩展点>.options` | `options` 的默认值 |
| `points.<扩展点>.optionsSchema` | `schemas/` 下的文件名 |

提供的扩展点：`spec-export`、`authz-endpoints`、`log-parse`、`static-tools`、`local-run`。

### 5.3 spec-export

| `options` | 说明 |
|---|---|
| `project` | 启动项目的 `.csproj` 路径 |
| `configuration` | 构建配置，默认 `Debug` |
| `swaggerDoc` | `SwaggerDoc` 注册的文档名 |
| `fakeSecretEnv` | 启动期必须存在的密钥类配置的环境变量名；扩展为每个变量生成 48 字节随机数的 base64 串作为假值，只放入导出子进程的环境 |

实现要点：

1. `common/build.py` 构建 `project`；构建产物落在 worktree 的 `bin/`、`obj/`，由被测项目的 `.gitignore` 忽略。导出发生在任何 agent 运行之前，此时只读 worktree 尚未被 `guards` 设为不可写。
2. 由 `csproj.py` 读出项目引用的 `Swashbuckle.AspNetCore.Swagger` 版本，与全局工具 `Swashbuckle.AspNetCore.Cli` 的版本比较，不一致时以 `tool-missing` 结束，`hint` 为 `dotnet tool update -g Swashbuckle.AspNetCore.Cli --version <版本>`。
3. 执行 `dotnet swagger tofile --output <outputFile> <构建产物中的程序集> <swaggerDoc>`。`tofile` 会执行 `Startup` 的服务注册，启动期需要的密钥由 `fakeSecretEnv` 提供假值。
4. 目标框架为 net8.0 及以上的项目，构建期导出受官方限制，扩展以 `not-applicable` 结束并在 `message` 中说明，由项目改用运行期导出的项目扩展。

示例项目中的取值：`swaggerDoc` 为 `V1`，与 `Startup.cs` 中 `SwaggerDoc("V1", ...)` 一致。

### 5.4 authz-endpoints

| `options` | 说明 |
|---|---|
| `project` | 启动项目的 `.csproj` 路径 |
| `assemblies` | 需要读取的程序集文件名，默认取 `project` 的输出程序集 |

`EndpointPolicies` 是随扩展提供的 C# 控制台工具：

1. 用 `System.Reflection.MetadataLoadContext` 只读加载程序集，不执行被测程序的任何代码。
2. 遍历继承 `ControllerBase` 的类型与其公开方法，读取 `[Route]`、`[HttpGet]` 等 HTTP 方法特性、`[Authorize(Policy = ...)]`、`[AllowAnonymous]`；类与方法上的特性按 ASP.NET Core 的规则合并(方法上的 `[AllowAnonymous]` 覆盖类上的 `[Authorize]`，多个 `[Authorize]` 的 Policy 同时需要)。
3. 路由模板中的 `[controller]`、`[action]` 替换为实际名称，路由约束(`{id:int}`)去掉约束部分，得到与接口描述一致的写法。
4. 用 `System.Reflection.Metadata` 读取同目录的 portable PDB，取每个方法第一个序列点所在的源文件作为 `sourceFile`；没有 PDB 时为空。
5. 输出 `endpoints`：Policy 名作为 `requires` 中的能力名；无法解析的端点列入 `unresolved`。

首次使用时以 `dotnet build` 把 `EndpointPolicies` 构建到 `TIGHTREIN_CACHE_DIR` 下按扩展版本区分的目录，之后直接复用。

### 5.5 log-parse

解析 `Microsoft.Extensions.Logging` 的 `simple` 控制台格式：

| `options` | 说明 |
|---|---|
| `timestampFormat` | 与项目日志配置的 `TimestampFormat` 一致，例如 `HH:mm:ss` |
| `timezone` | 服务器时区，IANA 名称 |
| `rolloverToleranceMinutes` | 跨日判定容差，默认 30 |
| `frameworkNamespaces` | 判为非本项目帧的命名空间前缀，默认 `System.`、`Microsoft.` |

| 规则 | 做法 |
|---|---|
| 条目切分 | 以「时间戳 + 级别前缀」开头的行作为一条日志的起点，前缀形式为 `<时间戳> <级别>: `；后续不以此开头的行并入上一条。`SingleLine: true` 时一条日志本应占一行，但 Windows 上换行符不一致时堆栈仍可能跨行输出，跨行合并同时覆盖这种情况 |
| 级别 | `trce`、`dbug`、`info`、`warn`、`fail`、`crit` 分别归一化为 `trace`、`debug`、`information`、`warning`、`error`、`critical` |
| 类别与事件编号 | 前缀后的 `<类别>[<事件编号>]` |
| 异常 | 匹配 `<异常类型>: <消息>`；堆栈帧匹配 `at <命名空间.类名.方法名>(...)`，可带 `in <文件>:line <行号>` |
| 本项目帧 | `symbol` 不以 `frameworkNamespaces` 中任一前缀开头的帧，`isProject` 为 `true` |
| 日期补齐 | 时间戳只有时分秒时，从 `state` 中该流的最后一条本地时间开始顺序推进；某条的时分秒比上一条小超过 `rolloverToleranceMinutes`，判为跨过午夜，日期加一；没有 `state` 时以片段的 `modifiedAt` 的日期为起点倒推。补齐后按 `timezone` 换算为 UTC |
| 片段开头的续行 | 不以条目前缀开头、又没有上一条可以并入的行计入 `unparsed` |

`state` 为「流名 → 最后一条本地日期时间」。

### 5.6 static-tools

| `options` | 说明 |
|---|---|
| `solution` | 构建与依赖漏洞检查所用的 `.sln` 或 `.csproj` |

| 工具名 | 命令 | 解析 |
|---|---|---|
| `dotnet-build` | `dotnet build <solution>` | `common/msbuild.py` 解析 `<文件>(<行>,<列>): warning <代码>: <消息>` 行，同一告警在多个目标框架下重复出现时只保留一条；`kind` 为 `build-warning`，`severity` 为 `low` |
| `dotnet-vulnerable` | `dotnet list <solution> package --vulnerable --include-transitive --format json` | 解析 JSON 中各项目、各框架下带漏洞的包；`kind` 为 `vulnerability`，`file` 为项目文件，`severity` 取输出中的严重度；这条命令发现漏洞时不以非零退出码结束，只以输出为准 |

- `--format json` 需要 .NET SDK 8 及以上，与被测项目的目标框架无关。
- `incremental` 档位下改动文件中没有 `.cs`、`.csproj`、`.props`、`.targets` 时两个工具都返回 `skipped`。

### 5.7 local-run

| `options` | 说明 |
|---|---|
| `project` | 启动项目的 `.csproj` 路径 |
| `env` | 追加的环境变量，例如 `ASPNETCORE_ENVIRONMENT` |
| `healthPath` | 用于确认端口可达的路径，默认 `/` |

输出一个名为 `backend` 的服务：

| 字段 | 取值 |
|---|---|
| `argv` | `dotnet run --project <project> --urls http://localhost:<ports.backend>` |
| `env` | `options.env` |
| `readyPatterns` | `Now listening on`、`Application started` |
| `failPatterns` | `Unhandled exception`、`error CS\d+`、`address already in use`、`Failed to bind to address` |
| `readyUrl` | `http://localhost:<ports.backend><healthPath>` |

`migrationPaths` 与 `unavailable` 为空，由项目扩展补充。

## 6. 示例项目的项目扩展

### 6.1 目录

```
workspaces/demo/extensions/
  authz_roles.py              authz-roles：解析 PermissionMatrix.cs
  log_parse_frames.py         log-parse(extend)：按命名空间识别本项目帧
  static_tools_frontend.py    static-tools(extend)：前端构建告警与 npm audit
  page_routes.mjs             page-routes：解析前端路由文件
  local_run_frontend.py       local-run(extend)：前端开发服务器、迁移文件与不在本机启动的服务
  tests/
    fixtures/<扩展点>/<用例>/  input.json、expected.json、files/
    test_<扩展点>.py
    page_routes.test.mjs
```

各扩展在 `project.yaml` 中的声明见 1.5 的「示例项目中的取值」。

### 6.2 authz-roles

1. 读取 `options.matrixFile`(示例项目中的取值：`src/Auth/PermissionMatrix.cs`)。
2. 从 `Permission` 静态类的 `public const string` 字段得到全部能力名，作为 `capabilities`。
3. 解析 `PermissionRule` 的对象初始化器，取出每条规则对应的能力与放行的角色集合，输出各角色(示例项目中为 `Admin`、`Manager`、`User`)对每个能力的放行结果。
4. `input.roles` 中的角色键与矩阵中的角色名按 `options.roleMap` 对应，未给出时按名称相同对应。
5. 遇到无法识别的初始化器写法时以 `parse-failed` 结束，`message` 给出文件与行号，不猜测。

### 6.3 服务端日志

按 redesign/01-collect.md，被测系统内部的错误只经它已接入的平台的只读查询 API 取得，不读服务器上的日志文件。示例项目的
后端日志接入集中日志平台后选用 `core/loki`(或以后补充的平台方法)，`log-parse` 照常用 `aspnetcore/dotnet-console` 加下面
的 extend。

### 6.4 log-parse(extend)

在 aspnetcore 的解析结果上重新判定本项目帧：`symbol` 以 `options.projectNamespaces` 中任一前缀加 `.` 开头的帧 `isProject` 为 `true`，其余为 `false`。示例项目中的取值：`projectNamespaces` 为 `Demo`。其余字段原样保留。

### 6.5 static-tools(extend)

在 aspnetcore 的结果上追加前端的两个工具，`options.frontendDir` 为 `src/vue`：

| 工具名 | 命令 | 解析 |
|---|---|---|
| `npm-build` | `npm run build` | vue-cli 输出中的 `warning in <文件>` 与其后的 `<行>:<列>` 行；`kind` 为 `build-warning` |
| `npm-audit` | `npm audit --json` | `vulnerabilities` 中的每个包；`file` 为 `src/vue/package.json`；有漏洞时退出码非零，属于正常结果 |

`incremental` 档位下改动文件中没有 `src/vue/` 下的文件时两个工具返回 `skipped`。依赖来自只读 worktree 中已安装的 `node_modules/`，被项目 `.gitignore` 忽略；缺失时以 `tool-missing` 结束，`hint` 为在只读 worktree 的 `src/vue` 执行 `npm ci`。

### 6.6 page-routes

1. 用前端依赖中已有的 `@babel/parser`(从 `options.frontendDir` 的 `node_modules` 解析)把 `options.routerFile`(示例项目中的取值：`src/vue/src/router/index.js`)解析为语法树，不执行文件。
2. 找到 `routes` 数组，递归遍历 `path` 与 `children`，子路由的相对路径拼接到父路由之后。
3. `component` 为 `() => import('<路径>')` 或已导入的标识符时，解析出 `.vue` 文件路径作为 `componentFile`；`meta` 中的字面量原样输出。
4. 只有 `redirect` 没有 `component` 的条目与 `*` 不列入。
5. `path` 不是字符串字面量的条目列入 `notes`，不猜测。

### 6.7 local-run(extend)

在 aspnetcore 给出的 `backend` 服务上补充：

| 项 | 示例项目中的取值 |
|---|---|
| `backend.env` | `ASPNETCORE_ENVIRONMENT=Development`：Development 环境连接的测试库与 staging 是同一个库；连接配置由程序自己读取，扩展不读取任何配置文件与凭证 |
| `page` 模式的 `frontend` 服务 | `argv` 为 `npm run serve -- --port <ports.frontend>`，`cwd` 为 `src/vue`，`after` 为 `backend`；`readyPatterns` 为 `App running at`，`failPatterns` 为 `Failed to compile`、`EADDRINUSE` |
| 端口约束 | 前端开发服务器把 `/api` 代理到 5000(`vue.config.js`)，所以 `page` 模式的 `ports.backend` 必须为 5000；`input.ports.backend` 不是 5000 时以 `invalid-input` 结束 |
| `unavailable` | `compute`：Python 计算服务不在本机启动 |
| `migrationPaths` | `src/Migrations/MigrationList.cs` |

## 7. 测试

### 7.1 扩展自身的测试

每个扩展用 JSON 夹具单独测试，不需要核心：

| 夹具文件 | 内容 |
|---|---|
| `input.json` | 完整的请求 JSON(2.2)；`repo` 等路径写成以 `{fixture}` 开头的相对路径，由测试替换为夹具目录 |
| `files/` | 扩展要读取的内容：源文件片段、日志样本、工具输出的录制 |
| `expected.json` | 期望的完整响应 JSON(2.3) |

- 需要外部工具的扩展把工具调用集中在一个函数中，测试时以录制的工具输出替代；以真实工具运行的测试放在 `tests/integration/`，工具缺失时跳过。
- 每个扩展至少覆盖：正常输出、`not-applicable`、所需输入缺失时的 `invalid-input`、无法解析时的 `parse-failed`。

| 扩展 | 夹具要点 |
|---|---|
| aspnetcore `spec-export` | 版本不符时的 `tool-missing`；net8.0 项目的 `not-applicable`；以 `sample-app` 做集成测试 |
| aspnetcore `authz-endpoints` | `sample-app`：类与方法特性的合并、`[AllowAnonymous]` 覆盖、`[controller]` 替换、路由约束去除、无 PDB 时 `sourceFile` 为空 |
| aspnetcore `log-parse` | 单行条目、堆栈跨行、午夜跨日、无 `state` 时按修改时间倒推、六种级别、框架帧与非框架帧 |
| aspnetcore `static-tools` | MSBuild 告警行(含多目标框架重复)、`dotnet list package` 的 JSON(有漏洞与无漏洞)、无相关改动时 `skipped` |
| aspnetcore `local-run` | 两种模式的输出、`options.env` 透传 |
| 示例项目 `authz-roles` | 矩阵文件的样本、新增能力、无法识别的写法 |
| 示例项目 `log-parse` | 以 `base` 为输入，`Demo` 帧与其他命名空间帧的判定 |
| 示例项目 `static-tools` | `base` 保留、vue-cli 告警输出、`npm audit` 的 JSON |
| 示例项目 `page-routes` | 嵌套路由拼接、动态导入与静态导入的组件、重定向与兜底路由的排除 |
| 示例项目 `local-run` | `page` 模式补充前端、`ports.backend` 不是 5000 时的 `invalid-input` |

### 7.2 核心的测试

| 对象 | 方式 |
|---|---|
| `resolve.py` | 三层查找、`enabled: false`、`extend` 缺少下一层、未知扩展点、`logQuery` 有值而没有 `log-parse` |
| `invoke.py` | 以夹具脚本模拟扩展：正常响应、`status: error`、非 0 退出无响应、输出非 JSON、超出大小、超时、`schema-invalid`；超时后进程组被终止 |
| `cache.py` | 缓存命中、缓存键变化后重算、HEAD 不在目标 commit 时不调用 |
| `defaults.py` | 4.1 每一行 |
| schema | `contracts/schemas/extension/points/` 中每个 schema 至少一个合法与一个非法样例，合法样例直接取 7.1 的 `expected.json` |

## 8. 命令行

| 命令 | 作用 |
|---|---|
| `tightrein admin ext list` | 列出每个扩展点的实现层、命令、生效的 `options` 与超时 |
| `tightrein admin ext run <扩展点> [--input <文件>] [--commit <commit>]` | 调用一次扩展点并输出经过校验的响应；`--input` 给出 `input` 部分的 JSON，省略时按当前工作区与只读 worktree 组装；不写缓存，不更新读取位置 |
| `tightrein admin ext test [--stack <技术栈>] [--workspace-extensions]` | 以 7.1 的夹具运行技术栈扩展或当前工作区的项目扩展，逐个比对 `expected.json` |

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
