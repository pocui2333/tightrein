# 方法目录

<!-- 本文件由 core/dev/contract_reference.py 生成，不要手改；改 schema 或类型注册后重新生成。 -->

各扩展点可选用的核心方法(architecture/10 1.4)。项目在 project.yaml 的 `extensions.<扩展点>.use` 中选用，`options` 填写参数；缺省值来自 config/defaults.yaml 的 `methods.<方法编号>`。每种方法默认不启用，配置了才启用。

## spec-export(导出接口描述)

### core/openapi-file

读取现成的 OpenAPI 或 Swagger 接口描述文件(JSON 或 YAML)，文件在仓库或工作区中

**适用条件**：仓库中提交了随代码更新的接口描述文件，或在工作区中维护了一份接口描述

**前提**：

- 仓库中有随代码更新的接口描述文件，或工作区中有一份确认过的接口描述(可由 tightrein spec draft 起草)

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `path` | 字符串(`^[^/\\][^\\]*$`) | 是 |  | 接口描述文件相对基准目录(base)的路径 |
| `base` | `repo` \| `workspace` | 否 | `"repo"` | path 的基准目录：repo 为仓库根目录(随 commit 取文件)，workspace 为工作区根目录 |

**输出**：本 commit 的接口描述(写到 data/specs/<commit>/openapi.json)

**限制**：

- 手写的接口描述视为不由框架导出，api-fuzz 的响应结构检查缺省关闭

**示例配置**：

```yaml
extensions:
  spec-export:
    use: core/openapi-file
    options: {path: openapi.yaml, base: workspace}
```

### core/openapi-url

从本机启动的服务读取接口描述(JSON 或 YAML)

**适用条件**：服务运行时提供接口描述地址，并且调用前已在本机启动

**前提**：

- 服务运行时提供接口描述地址(框架自动导出，如 Swagger)，调用前已在本机启动

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `url` | 字符串(`^https?://`) | 是 |  | 接口描述的地址，例如 http://localhost:8000/openapi.json |
| `timeoutSeconds` | 整数 | 否 | `30` | 请求的超时秒数 |

**输出**：本 commit 的接口描述

**限制**：

- 需要本机启动服务(local-run)

**示例配置**：

```yaml
extensions:
  spec-export:
    use: core/openapi-url
    options: {url: "http://localhost:8000/openapi.json"}
```

## authz-endpoints(端点所需能力)

### core/manual-list

读取工作区中手工维护的端点权限清单(YAML)

**适用条件**：代码中的权限声明无法自动读出，端点数量不多，可以随接口变化手工维护

**前提**：

- 在工作区写一份端点清单，每个端点列出方法、路由模板与所需能力

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `file` | 字符串(`^[^/\\][^\\]*$`) | 是 |  | 清单文件相对工作区根目录的路径，例如 authz/endpoints.yaml |

**输出**：每个端点所需的能力(能力名为任意字符串，与 authz-roles 的能力名一致)

**限制**：

- 清单随接口变化需要手工更新，过期的清单会让越权检查误报或漏报

**示例配置**：

```yaml
extensions:
  authz-endpoints:
    use: core/manual-list
    options: {file: authz/endpoints.yaml}
```

### core/openapi-security

从接口描述的 security 声明推出每个端点所需的能力，作用域名即能力名

**适用条件**：接口描述为每个端点写明了 security 要求，并且用作用域(scope)表示所需的权限

**前提**：

- 接口描述为每个端点写了 security 要求，作用域(scope)表示所需的权限

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `path` | 字符串(`^[^/\\][^\\]*$`) | 否 |  | 接口描述文件相对仓库根目录的路径；省略时取本 commit 的 spec-export 结果 |

**输出**：每个端点所需的能力，作用域名即能力名；没有 security 的端点视为不需要能力

**限制**：

- 只读接口描述，不看代码；接口描述与代码不一致时以接口描述为准

**示例配置**：

```yaml
extensions:
  authz-endpoints:
    use: core/openapi-security
```

## authz-roles(角色具备的能力)

### core/manual-matrix

读取工作区中手工维护的角色能力矩阵(YAML)

**适用条件**：角色与能力的对应关系无法从代码自动读出，可以随权限变化手工维护

**前提**：

- 在工作区写一份矩阵，列出每个角色具备的能力

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `file` | 字符串(`^[^/\\][^\\]*$`) | 是 |  | 矩阵文件相对工作区根目录的路径，例如 authz/roles.yaml |

**输出**：每个角色(accounts.roles 中的角色)具备的能力

**限制**：

- 矩阵随权限变化需要手工更新

**示例配置**：

```yaml
extensions:
  authz-roles:
    use: core/manual-matrix
    options: {file: authz/roles.yaml}
```

## error-tracking(读取错误追踪平台)

### core/sentry

读取 Sentry 中时间窗口内有新事件的错误分组(后端异常与浏览器端错误，含堆栈与操作轨迹)，只读 API

**适用条件**：被测项目已接入 Sentry SDK(后端或浏览器端)；自建 Sentry 或兼容其 API 的 GlitchTip 改 baseUrl 即可

**前提**：

- 在 Sentry 建一个只有 event:read 权限的令牌(Organization Token 或 Internal Integration)
- security add-generic-password -s <条目名> -a sentry -w 把令牌存进钥匙串
- 前端错误要还原到源码行，被测项目构建时须向 Sentry 上传 source map

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `baseUrl` | 字符串(`^https?://`) | 否 | `"https://sentry.io"` | Sentry 的地址，SaaS 为 https://sentry.io |
| `organization` | 字符串 | 是 |  | 组织的 slug |
| `projects` | 数组 | 否 | `[]` | 项目的 slug 或编号；为空时为组织下全部项目 |
| `environment` | 字符串 或 null | 否 | `null` | 只看这个环境；为空时不限 |
| `keychainItem` | 字符串 | 是 |  | 存放只读令牌(event:read)的钥匙串条目名 |
| `retentionDays` | 整数 | 否 | `90` | 平台的数据保留天数，用于判断读取窗口是否超出保留期 |
| `limit` | 整数 | 否 | `100` | 每次读取的分组条数上限 |
| `breadcrumbs` | 整数 | 否 | `10` | 每个分组保留的最近操作轨迹条数 |
| `timeoutSeconds` | 整数 | 否 | `30` | 每个请求的超时秒数 |

**输出**：每个分组一条，含分组编号(sentry:<组织>/<编号>，作为问题指纹)、前后端、标题、异常类型与消息、出现次数、影响用户数、首末出现时间、版本、堆栈帧(出错处在前)、操作轨迹、页面地址与浏览器

**限制**：

- 每个分组另发一次请求取最新事件，limit 不宜过大(受 Sentry 的速率限制)
- 只读 is:unresolved 的分组；平台上的解决状态不回写也不读取，问题的解决按覆盖运行判断

**示例配置**：

```yaml
extensions:
  error-tracking:
    use: core/sentry
    options: {organization: my-org, projects: [api, web], keychainItem: tightrein.my-project.sentry}
```

## log-platform(查询集中日志平台)

### core/loki

在 Grafana Loki 上按 LogQL 查询时间窗口内的日志原文，只读 API

**适用条件**：应用运行日志、项目规范的日志或访问日志已送入 Grafana Loki(自建或 Grafana Cloud)

**前提**：

- 在 Grafana Cloud 建一个只有 logs:read 的访问策略令牌，或给自建 Loki 配只读的反向代理认证
- security add-generic-password -s <条目名> -a loki -w 把令牌存进钥匙串
- 日志的字段映射另选 log-parse 方法(core/json-lines 或 core/regex)

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `url` | 字符串(`^https?://`) | 是 |  | Loki 的地址(Grafana Cloud 为 https://logs-prod-<区域>.grafana.net) |
| `user` | 字符串 或 null | 否 | `null` | 基本认证的用户名(Grafana Cloud 为实例编号)；为空时令牌按 Bearer 发送 |
| `tenant` | 字符串 或 null | 否 | `null` | 多租户部署的 X-Scope-OrgID；为空时不带 |
| `keychainItem` | 字符串 或 null | 否 | `null` | 存放只读访问令牌的钥匙串条目名；为空时不带认证 |
| `retentionDays` | 整数 | 否 | `30` | 平台的数据保留天数，用于判断读取窗口是否超出保留期 |
| `pageSize` | 整数 | 否 | `1000` | 每次请求的条数 |
| `timeoutSeconds` | 整数 | 否 | `30` | 每个请求的超时秒数 |

**输出**：按标签组合(流)分成片段，片段正文为窗口内按时间排列的原文行，交给 log-parse 解析

**限制**：

- 只支持日志查询(LogQL 的流选择器与过滤)，不支持指标查询
- 同一纳秒的多条日志跨页时可能少读，条数上限内读完的窗口不受影响

**示例配置**：

```yaml
extensions:
  log-platform:
    use: core/loki
    options: {url: "https://logs-prod-012.grafana.net", user: "123456", keychainItem: tightrein.my-project.loki}
  log-parse:
    use: core/json-lines
    options: {timeField: ts, levelField: level, messageField: msg, exceptionTypeField: error.type}
sources:
  platform-errors:
    logQuery: '{app="api"} | json | level=~"error|fatal"'
```

## log-parse(解析日志)

### core/json-lines

解析每行一个 JSON 对象的结构化日志，字段位置可配置

**适用条件**：服务以 JSON Lines 输出日志(常见于结构化日志库)

**前提**：

- 日志平台(extensions.log-platform)取回的原文是每行一个 JSON 对象

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `timeField` | 字符串 | 否 | `"timestamp"` | 时间字段，嵌套字段以 . 连接 |
| `levelField` | 字符串 | 否 | `"level"` | 级别字段 |
| `messageField` | 字符串 | 否 | `"message"` | 消息字段 |
| `categoryField` | 字符串 或 null | 否 | `null` | 类别字段，可空 |
| `eventIdField` | 字符串 或 null | 否 | `null` | 事件编号字段，可空 |
| `exceptionTypeField` | 字符串 或 null | 否 | `null` | 异常类型字段，可空 |
| `exceptionMessageField` | 字符串 或 null | 否 | `null` | 异常消息字段，可空 |
| `timezone` | 字符串 | 否 | `"UTC"` | 不带时区的时间所用的时区(IANA 名称) |
| `levels` | 对象 | 否 | `{"trace": "trace", "trce": "trace", "verbose": "trace", "vrb": "trace", "debug": "debug", "dbug": "debug", "dbg": "debug", "information": "information", "info": "information", "inf": "information", "notice": "information", "warning": "warning", "warn": "warning", "wrn": "warning", "error": "error", "err": "error", "fail": "error", "critical": "critical", "crit": "critical", "crt": "critical", "fatal": "critical", "ftl": "critical"}` | 级别映射，键为原文中的写法(不区分大小写)，值为归一化级别 |

**输出**：日志条目：时间(UTC)、归一化级别、类别、事件编号、消息、异常类型与消息、原文

**限制**：

- 不解析堆栈帧；需要本项目帧时选用技术栈方法或以 extend 补充

**示例配置**：

```yaml
extensions:
  log-parse:
    use: core/json-lines
    options: {timeField: ts, levelField: level, messageField: msg, exceptionTypeField: error.type}
```

### core/regex

按正则的命名分组从每条日志的首行抽取时间、级别、类别与消息，续行并入上一条

**适用条件**：日志是逐行的文本格式，每条日志的首行有固定的写法

**前提**：

- 每条日志的首行有固定写法，能用正则的命名分组 time、level、message 取出

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `pattern` | 字符串 | 是 |  | 从行首匹配的正则，须有命名分组 time、level、message，可选 category、eventId |
| `timeFormat` | 字符串 或 null | 否 | `null` | time 分组的 strptime 写法；为空时按 ISO 8601 |
| `timezone` | 字符串 | 否 | `"UTC"` | 不带时区的时间所用的时区(IANA 名称) |
| `levels` | 对象 | 否 | `{"trace": "trace", "trce": "trace", "verbose": "trace", "vrb": "trace", "debug": "debug", "dbug": "debug", "dbg": "debug", "information": "information", "info": "information", "inf": "information", "notice": "information", "warning": "warning", "warn": "warning", "wrn": "warning", "error": "error", "err": "error", "fail": "error", "critical": "critical", "crit": "critical", "crt": "critical", "fatal": "critical", "ftl": "critical"}` | 级别映射，键为原文中的写法(不区分大小写)，值为归一化级别 |
| `multiline` | 布尔 | 否 | `true` | 不匹配的行是否并入上一条 |
| `rolloverToleranceMinutes` | 整数 | 否 | `30` | 只有时刻时判定跨过午夜的容差(分钟) |

**输出**：日志条目，续行(堆栈等)并入上一条的原文

**限制**：

- 时间只有时刻时按读取顺序补齐日期，跨天的乱序日志可能算错日期

**示例配置**：

```yaml
extensions:
  log-parse:
    use: core/regex
    options: {pattern: '^(?P<time>\S+ \S+) (?P<level>[A-Z]+) (?P<message>.*)$', timeFormat: '%Y-%m-%d %H:%M:%S'}
```

## alert-source(读取业务告警)

### core/alertmanager

经 Alertmanager API v2 读取已触发、未静默的业务告警，只读 API

**适用条件**：被测系统已有 Prometheus 或 Grafana 的告警规则(Prometheus Alertmanager、Grafana 内置告警、Grafana Cloud、Mimir)

**前提**：

- 告警规则已在平台上配置并会触发；tightrein 不重复实现告警规则
- Grafana 的服务账号令牌只需 Viewer 角色(读取告警)
- security add-generic-password -s <条目名> -a alertmanager -w 把令牌存进钥匙串

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `url` | 字符串(`^https?://`) | 是 |  | Alertmanager 的地址；Grafana 内置告警为 <Grafana 地址>/api/alertmanager/grafana |
| `keychainItem` | 字符串 或 null | 否 | `null` | 存放只读令牌的钥匙串条目名；为空时不带认证 |
| `user` | 字符串 或 null | 否 | `null` | 基本认证的用户名；为空时令牌按 Bearer 发送 |
| `timeoutSeconds` | 整数 | 否 | `30` | 请求的超时秒数 |

**输出**：每条已触发、未静默、未被抑制的告警，含 fingerprint(作为问题指纹)、告警名、标签、注解、开始时间与来源链接

**限制**：

- 只读当前处于触发状态的告警，两次运行之间触发又恢复的告警读不到
- 基础设施类告警按 sources.alerts.exclude 的名称与标签排除，项目的标签约定不同时需要调整

**示例配置**：

```yaml
extensions:
  alert-source:
    use: core/alertmanager
    options: {url: "https://grafana.example.com/api/alertmanager/grafana", keychainItem: tightrein.my-project.grafana}
```

## static-tools(技术栈检查工具)

### core/semgrep

以 Semgrep 按给定的规则扫描改动文件(full 档位扫描整个仓库)

**适用条件**：需要在 static 探针自带的 Semgrep 之外，以另一组规则作为确定性工具运行

**前提**：

- 本机可以运行 Semgrep(runtime.tools.semgrep 或本机用户配置的 tools.semgrep.path)

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `configs` | 数组 | 是 |  | Semgrep 的 --config 取值，例如规则文件路径或 p/<规则集> |
| `timeoutSeconds` | 整数 | 否 | `1500` | Semgrep 的超时秒数，应小于 static-tools 的超时 |

**输出**：确定性工具的结果，交给静态巡检的审查

**限制**：

- 只扫描改动文件(full 档位扫描整个仓库)

**示例配置**：

```yaml
extensions:
  static-tools:
    use: core/semgrep
    options: {configs: [p/python]}
```

## page-routes(前端页面路由)

### core/manual-list

读取工作区中手工维护的页面路由清单(YAML)

**适用条件**：前端路由无法静态解析，页面数量不多，可以随页面变化手工维护

**前提**：

- 在工作区写一份页面路由清单

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `file` | 字符串(`^[^/\\][^\\]*$`) | 是 |  | 清单文件相对工作区根目录的路径，例如 e2e/routes.yaml |

**输出**：页面路由模板清单，供验证环节判断改动影响的页面

**限制**：

- 清单随页面变化需要手工更新

**示例配置**：

```yaml
extensions:
  page-routes:
    use: core/manual-list
    options: {file: pages.yaml}
```

## local-run(本机启动计划)

### core/command-sequence

按列出的顺序启动一组命令，逐个等待就绪信号；端口按模式取自 localRun.ports

**适用条件**：被测服务可以用固定的命令在 worktree 中启动，并能从日志或 HTTP 地址判断就绪

**前提**：

- 被测服务可以用固定的命令在 worktree 中启动，localRun.ports 给出各模式的端口

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `services` | 数组 | 是 |  |  |
| `unavailable` | 数组 | 否 | `[]` | 不在本机启动的服务 |
| `migrationPaths` | 数组 | 否 | `[]` | 迁移文件的路径模式 |

**输出**：启动计划：服务的命令、工作目录、非敏感的环境变量、端口与就绪信号

**限制**：

- 改动命中 migrationPaths 时启动前需要用户确认；环境变量不能含凭证

**示例配置**：

```yaml
extensions:
  local-run:
    use: core/command-sequence
    options:
      services:
        - {name: api, argv: [dotnet, run, --urls, "http://localhost:{port}"], port: backend,
           readyPatterns: ["Now listening"]}
```

## deploy-source(读取部署记录)

### core/github-actions

读取部署工作流(GitHub Actions)最近的运行，作为部署记录

**适用条件**：项目用一个 GitHub Actions 工作流部署，没有使用 GitHub Deployments

**前提**：

- 本机已安装并登录 GitHub CLI(gh auth login)，账号对仓库有读权限

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `workflow` | 字符串 | 是 |  | 部署工作流的文件名或名称，例如 deploy-staging.yml |
| `branch` | 字符串 或 null | 否 | `null` | 部署工作流运行的分支；为空时取请求中的分支(project.mainBranch) |
| `limit` | 整数 | 否 | `20` | 每次读取的最近运行条数 |
| `timeoutSeconds` | 整数 | 否 | `60` | gh 命令的超时秒数 |

**输出**：部署工作流最近的运行，每条含编号、commit、状态(succeeded、failed、running、skipped)、地址与时间

**限制**：

- 只看指定的工作流；同一工作流内多个环境的部署无法区分

**示例配置**：

```yaml
extensions:
  deploy-source:
    use: core/github-actions
    options: {workflow: deploy.yml}
```

### core/github-deployments

读取 GitHub Deployments 中最近的部署与各自最新的状态

**适用条件**：部署平台或工作流会写 GitHub Deployments(GitHub 页面上的 Environments)

**前提**：

- 本机已安装并登录 GitHub CLI，账号对仓库有读权限
- 部署平台或工作流会写 GitHub Deployments

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `environment` | 字符串 或 null | 否 | `null` | 只看这个环境的部署；为空时不限 |
| `limit` | 整数 | 否 | `20` | 每次读取的最近部署条数 |
| `timeoutSeconds` | 整数 | 否 | `60` | 每条 gh 命令的超时秒数 |

**输出**：最近的部署与各自最新的状态

**限制**：

- 没有写 Deployments 的部署方式读不到

**示例配置**：

```yaml
extensions:
  deploy-source:
    use: core/github-deployments
    options: {environment: production}
```

### core/vercel

读取 Vercel 项目最近的部署(Vercel REST API，只读)

**适用条件**：项目部署在 Vercel，部署由 Git 集成触发(部署记录带 Git commit)

**前提**：

- 在 Vercel 建一个只读的访问令牌，用 security add-generic-password -s <条目名> -a vercel -w 存进钥匙串

**参数**：

| 参数 | 类型 | 必填 | 缺省值 | 说明 |
|---|---|---|---|---|
| `projectId` | 字符串 | 是 |  | Vercel 项目的 ID 或名称 |
| `keychainItem` | 字符串 | 是 |  | 存放 Vercel 访问令牌的 macOS 钥匙串条目名(security add-generic-password -s <条目名> -w) |
| `target` | `production` \| `preview` | 否 | `"production"` | 部署的 target：production 或 preview |
| `teamId` | 字符串 或 null | 否 | `null` | 团队项目的 teamId；个人项目为空 |
| `limit` | 整数 | 否 | `20` | 每次读取的最近部署条数 |
| `timeoutSeconds` | 整数 | 否 | `30` | 请求 Vercel API 的超时秒数 |

**输出**：最近的部署，每条含 commit、状态、target、地址与时间；没有 Git commit 的手动部署不计入

**限制**：

- 只支持由 Git 集成触发的部署

**示例配置**：

```yaml
extensions:
  deploy-source:
    use: core/vercel
    options: {projectId: my-site, keychainItem: tightrein.my-site.vercel}
```

## 后续平台

以下平台留作后续，按方法目录的写法新增方法即可，不改核心：错误追踪 Rollbar、Bugsnag、Honeybadger；集中日志 Elasticsearch/OpenSearch、云厂商的日志服务(CloudWatch Logs、阿里云 SLS 等)、Datadog Logs；业务告警 Grafana 告警的 Ruler API、云监控的告警接口。
