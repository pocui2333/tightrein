# 平台方法

> 本文件由 `src/tightrein/cli/reference.py` 生成，不要手改；改了方法清单(`<方法>.yaml`)后运行 `python -m tightrein.cli.reference`。

每个方法是一个程序加一个同名清单，放在模块的方法目录下；接入清单 `setup.json` 中该模块的 `method` 写方法名。参数合并自 `sites.json` 的 `site` 分组、settings 中该来源控制键下以方法名为键的一节，按 `optionsSchema` 校验；凭据取 `secrets.json` 的 `secret` 条目。加一种方法见 `docs/how-to/add-method.md`。

| 方法 | 目录 | 做什么 |
|---|---|---|
| [`alertmanager`](#collectalertsalert_sourcealertmanageryaml) | `collect/alerts/alert_source/` | 经 Alertmanager API v2 读取已触发、未静默、未被抑制的业务告警，只读 API |
| [`openapi_file`](#collectapi_fuzzspec_sourceopenapi_fileyaml) | `collect/api_fuzz/spec_source/` | 读取现成的 OpenAPI 或 Swagger 接口描述文件(JSON 或 YAML)，文件在仓库或工作区中 |
| [`openapi_url`](#collectapi_fuzzspec_sourceopenapi_urlyaml) | `collect/api_fuzz/spec_source/` | 从运行中的服务读取接口描述(JSON 或 YAML) |
| [`sentry`](#collectplatform_errorserror_trackingsentryyaml) | `collect/platform_errors/error_tracking/` | 读取 Sentry 中时间窗口内有新事件的错误分组(后端异常与浏览器端错误，含堆栈与操作轨迹)，只读 API |
| [`json_lines`](#collectplatform_errorslog_parsejson_linesyaml) | `collect/platform_errors/log_parse/` | 解析每行一个 JSON 对象的结构化日志，字段位置可配置 |
| [`regex`](#collectplatform_errorslog_parseregexyaml) | `collect/platform_errors/log_parse/` | 按正则的命名分组从每条日志的首行抽取时间、级别、类别与消息，续行并入上一条 |
| [`loki`](#collectplatform_errorslog_platformlokiyaml) | `collect/platform_errors/log_platform/` | 在 Grafana Loki 上按 LogQL 查询时间窗口内的日志原文，只读 API |
| [`semgrep`](#collectstatictoolssemgrepyaml) | `collect/static/tools/` | 以 Semgrep 按配置的规则集扫描检查范围；同一程序也用来跑工作区 rules/ 下的规则库 |
| [`github_actions`](#releasedeploy_sourcegithub_actionsyaml) | `release/deploy_source/` | 把部署工作流在主干上的运行当作部署记录(gh run list，只读) |
| [`github_deployments`](#releasedeploy_sourcegithub_deploymentsyaml) | `release/deploy_source/` | 读取 GitHub Deployments 与各自最新的状态(gh api，只读) |
| [`vercel`](#releasedeploy_sourcevercelyaml) | `release/deploy_source/` | 读取 Vercel 项目的部署列表(REST API，只读) |

## collect/alerts/alert_source/alertmanager.yaml

经 Alertmanager API v2 读取已触发、未静默、未被抑制的业务告警，只读 API

- 适用：系统已有 Prometheus 或 Grafana 的告警规则(Prometheus Alertmanager、Grafana 内置告警、Grafana Cloud、Mimir)
- 地址：sites.json 的 `alertmanager`
- 凭据：secrets.json 的 `alertmanager.token`
- 凭据必填：否

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | 字符串(`^https?://`) | 是 | Alertmanager 的地址；Grafana 内置告警为 <Grafana 地址>/api/alertmanager/grafana；写在 sites.json |
| `user` | 字符串 或 null | 否 | 基本认证的用户名；为空时令牌按 Bearer 发送；写在 sites.json |

限制：

- 只读当前处于触发状态的告警，两次运行之间触发又恢复的告警读不到
- 告警规则在平台上配置，tightrein 不重复实现告警规则；Grafana 的服务账号令牌只需 Viewer 角色

## collect/api_fuzz/spec_source/openapi_file.yaml

读取现成的 OpenAPI 或 Swagger 接口描述文件(JSON 或 YAML)，文件在仓库或工作区中

- 适用：仓库中提交了随代码更新的接口描述文件，或在工作区中维护了一份接口描述

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | 字符串(`^[^/\\][^\\]*$`) | 是 | 接口描述文件相对基准目录(base)的路径；写在工作区 settings.json 的 overrides.controls."collect.api_fuzz".openapi_file |
| `base` | `"repo"` \| `"workspace"` | 是 | path 的基准目录：repo 为仓库根目录(按被测部署的 commit 读)，workspace 为工作区根目录 |

限制：

- 取自仓库的按 commit 缓存在 data/cache/openapi/；取自工作区的每次重新读
- 文件不存在时 api_fuzz 本次跳过(不是失败)

示例：

```json
"overrides": {"controls": {"collect.api_fuzz": {"openapi_file": {"path": "openapi.yaml", "base": "repo"}}}}
```

## collect/api_fuzz/spec_source/openapi_url.yaml

从运行中的服务读取接口描述(JSON 或 YAML)

- 适用：服务运行时提供接口描述地址(框架自动导出，如 Swagger)

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | 字符串(`^https?://`) | 是 | 接口描述的地址，例如 https://staging.example.com/openapi.json；写在工作区 settings.json 的 overrides.controls."collect.api_fuzz".openapi_url |
| `timeoutSeconds` | 整数 | 是 | 请求的超时秒数(缺省见 settings/defaults.json) |

限制：

- 需要服务在运行；每次重新读，不缓存

示例：

```json
"overrides": {"controls": {"collect.api_fuzz": {"openapi_url": {"url": "https://staging.example.com/openapi.json"}}}}
```

## collect/platform_errors/error_tracking/sentry.yaml

读取 Sentry 中时间窗口内有新事件的错误分组(后端异常与浏览器端错误，含堆栈与操作轨迹)，只读 API

- 适用：项目已接入 Sentry SDK(后端或浏览器端)；自建 Sentry 或兼容其 API 的 GlitchTip 改 url 即可
- 地址：sites.json 的 `sentry`
- 凭据：secrets.json 的 `sentry.token`
- 凭据必填：是

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | 字符串(`^https?://`) | 是 | Sentry 的地址，SaaS 为 https://sentry.io；写在 sites.json |
| `organization` | 字符串 | 是 | 组织的 slug；写在 sites.json |
| `projects` | 数组(元素：字符串) | 是 | 项目的 slug 或编号；为空时为组织下全部项目 |
| `environment` | 字符串 或 null | 是 | 只看这个环境；为空时不限 |
| `retentionDays` | 整数 | 是 | 平台的数据保留天数，用于判断读取窗口是否超出保留期 |
| `limit` | 整数 | 是 | 每次读取的分组条数上限 |
| `breadcrumbs` | 整数 | 是 | 每个分组保留的最近操作轨迹条数 |

限制：

- 每个分组另发一次请求取最新事件，limit 不宜过大(受 Sentry 的速率限制)
- 只读 is:unresolved 的分组；平台上的解决状态不回写也不读取，问题的解决按覆盖运行判断
- 前端错误要还原到源码行，项目构建时须向 Sentry 上传 source map

## collect/platform_errors/log_parse/json_lines.yaml

解析每行一个 JSON 对象的结构化日志，字段位置可配置

- 适用：服务以 JSON Lines 输出日志(常见于结构化日志库)，日志平台取回的原文是每行一个 JSON 对象

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `timeField` | 字符串 | 是 | 时间字段，嵌套字段以 . 连接 |
| `levelField` | 字符串 | 是 | 级别字段 |
| `messageField` | 字符串 | 是 | 消息字段 |
| `categoryField` | 字符串 或 null | 是 | 类别字段；为空时不取 |
| `eventIdField` | 字符串 或 null | 是 | 事件编号字段；为空时不取 |
| `exceptionTypeField` | 字符串 或 null | 是 | 异常类型字段；为空时不取 |
| `exceptionMessageField` | 字符串 或 null | 是 | 异常消息字段；为空时不取 |
| `timezone` | 字符串 | 是 | 不带时区的时间所用的时区(IANA 名称) |
| `levels` | 对象 | 是 | 级别映射，键为原文中的写法(不区分大小写)，值为归一化级别 |

限制：

- 不解析堆栈帧
- 时间可以是 ISO 8601 字符串或 Unix 时间戳(秒；大于 10^11 时按毫秒)

## collect/platform_errors/log_parse/regex.yaml

按正则的命名分组从每条日志的首行抽取时间、级别、类别与消息，续行并入上一条

- 适用：日志是逐行的文本格式，每条日志的首行有固定的写法

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pattern` | 字符串 | 是 | 从行首匹配的正则，须有命名分组 time、level、message，可选 category、eventId |
| `timeFormat` | 字符串 或 null | 是 | time 分组的 strptime 写法；为空时按 ISO 8601 |
| `timezone` | 字符串 | 是 | 不带时区的时间所用的时区(IANA 名称) |
| `levels` | 对象 | 是 | 级别映射，键为原文中的写法(不区分大小写)，值为归一化级别 |
| `multiline` | 布尔 | 是 | 不以 pattern 开头的行是否并入上一条 |
| `rolloverToleranceMinutes` | 整数 | 是 | 只有时刻时判定跨过午夜的容差(分钟) |

限制：

- 时间只有时刻时按读取顺序补日期，跨天的乱序日志可能算错日期
- 不解析堆栈帧，续行只并入原文

## collect/platform_errors/log_platform/loki.yaml

在 Grafana Loki 上按 LogQL 查询时间窗口内的日志原文，只读 API

- 适用：应用运行日志、项目规范的日志或访问日志已送入 Grafana Loki(自建或 Grafana Cloud)
- 地址：sites.json 的 `loki`
- 凭据：secrets.json 的 `loki.token`
- 凭据必填：否

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | 字符串(`^https?://`) | 是 | Loki 的地址(Grafana Cloud 为 https://logs-prod-<区域>.grafana.net)；写在 sites.json |
| `user` | 字符串 或 null | 否 | 基本认证的用户名(Grafana Cloud 为实例编号)；为空时令牌按 Bearer 发送；写在 sites.json |
| `tenant` | 字符串 或 null | 否 | 多租户部署的 X-Scope-OrgID；为空时不带；写在 sites.json |
| `retentionDays` | 整数 | 是 | 平台的数据保留天数，用于判断读取窗口是否超出保留期 |
| `pageSize` | 整数 | 是 | 每次请求的条数 |

限制：

- 只支持日志查询(LogQL 的流选择器与过滤)，不支持指标查询
- 同一纳秒的多条日志跨页时可能少读，条数上限内读完的窗口不受影响
- 字段映射另选日志解析方法(log_parse/json_lines 或 log_parse/regex)

## collect/static/tools/semgrep.yaml

以 Semgrep 按配置的规则集扫描检查范围；同一程序也用来跑工作区 rules/ 下的规则库

- 适用：本机能运行 Semgrep(settings 的 tools.semgrep.path，或 PATH 中的 semgrep)

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `configs` | 数组(元素：字符串) | 是 | Semgrep 的 --config 取值，例如 p/python、p/javascript 或规则文件路径；为空时不运行 |
| `timeout` | 字符串 | 是 | 一次 Semgrep 的时限，如 30m |

限制：

- 增量档只扫改动文件；全量、基线与第一次扫整个仓库
- 只有退出码 0 算正常(不加 --error)，其他退出码按失败，结果丢弃、巡检记为 partial
- 不联网：--metrics=off，并关闭版本检查

示例：

```json
"overrides": {"controls": {"collect.static": {"semgrep": {"configs": ["p/python"]}}}}
```

## release/deploy_source/github_actions.yaml

把部署工作流在主干上的运行当作部署记录(gh run list，只读)

- 适用：项目用 GitHub Actions 的工作流部署(合并到主干后由某个工作流发布)；gh 已登录且能读该仓库

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `workflow` | 字符串 | 是 | 部署工作流的文件名或名称(如 deploy.yml)；写在 controls."release.deploy".github_actions |
| `branch` | 字符串 或 null | 是 | 只看这个分支上的运行；null 时取项目的主干分支 |
| `limit` | 整数 | 是 | 每次读取的运行条数上限 |

限制：

- 工作流的一次运行算一次部署：同一工作流里还有测试等非部署任务时，以整个运行的结论为准
- skipped 的运行不算失败，由之后包含该提交的运行确认上线

## release/deploy_source/github_deployments.yaml

读取 GitHub Deployments 与各自最新的状态(gh api，只读)

- 适用：部署流程会在 GitHub 上建 Deployment 并回写状态(GitHub Actions 的 environment、Vercel 与 Netlify 的 GitHub 集成等)；gh 已登录且能读该仓库

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `environment` | 字符串 或 null | 是 | 只看这个环境(如 production)；null 时不限 |
| `limit` | 整数 | 是 | 每次读取的部署条数上限 |

限制：

- 每个部署另发一次请求取最新状态，limit 不宜过大(受 GitHub 的速率限制)
- 部署流程不回写状态时一直算 running

## release/deploy_source/vercel.yaml

读取 Vercel 项目的部署列表(REST API，只读)

- 适用：项目部署在 Vercel，且部署由 Git 集成触发(手动上传的部署没有 commit，不计入)
- 凭据：secrets.json 的 `vercel.token`
- 凭据必填：是

参数：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `projectId` | 字符串 | 是 | Vercel 项目编号(prj_...)；写在 controls."release.deploy".vercel |
| `target` | 字符串 | 是 | 部署目标，通常为 production |
| `teamId` | 字符串 或 null | 是 | 项目属于团队时的团队编号；个人项目为 null |
| `limit` | 整数 | 是 | 每次读取的部署条数上限 |

限制：

- 只看 target 指定的部署目标；预览部署不计入
- 令牌需要读取该项目部署的权限
