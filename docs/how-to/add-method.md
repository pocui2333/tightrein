# 加一种方法

接一个新平台(另一种错误追踪、日志平台、告警系统、日志格式、接口描述来源)时，在对应模块的方法目录下加一对文件：一个程序 `<方法>.py`，加一个同名的方法清单 `<方法>.yaml`。调用方按 `setup.json` 或 settings 里写的方法名动态加载，不用改调用方，也不用改 `setup.json` 的校验(实现原则「通用，不写特例」)。

加载、参数合并与校验在 `src/tightrein/protocol/methods.py`；现有的全部方法与参数见 [平台方法](../reference/methods.md)。

## 1. 选方法目录

| 目录(`src/tightrein/` 下) | 方法名写在哪里 | 程序要提供 | 现有方法 |
|---|---|---|---|
| `collect/platform_errors/error_tracking/` | `setup.json` 中 `collect.platform_errors.method`(可与日志平台用 `+` 连写，如 `sentry+loki`) | `read(configured, *, transport, timeout_s, since, until, now)`，返回分组列表、是否读满上限、平台能查到的最早时间 | `sentry` |
| `collect/platform_errors/log_platform/` | 同上；访问日志也用这里的方法(`collect.access_log.method`) | `read(configured, *, transport, timeout_s, query, since, until, limit, now)`，返回日志片段(`chunks.Chunk`)、是否读满、最早时间 | `loki` |
| `collect/platform_errors/log_parse/` | `controls."collect.platform_errors".logParse` | `parse(chunks, options, state, now)`，返回解析出的条目与留给下次的状态 | `json_lines`、`regex` |
| `collect/alerts/alert_source/` | `setup.json` 中 `collect.alerts.method` | `read(configured, *, transport, timeout_s)`，返回告警列表 | `alertmanager` |
| `collect/api_fuzz/spec_source/` | `setup.json` 中 `collect.api_fuzz.method` | `fetch(request)`、`cache_key(request)`、`describe(request)` | `openapi_file`、`openapi_url` |

返回的类型照同目录已有方法的写法(如 `sentry.py` 的 `TrackingRead`、`loki.py` 的 `LogRead`)，调用方只认这些字段。

## 2. 写方法清单 `<方法>.yaml`

清单只声明，不写取值：

```yaml
name: graylog                       # 与文件名相同；只许小写字母、数字、下划线
kind: log_platform                  # 所在目录
summary: 在 Graylog 上按查询读取时间窗口内的日志原文，只读 API
applicability: 日志集中在 Graylog 5 以上，有只读的 API 令牌
site: graylog                       # 地址等取自 sites.json 的 graylog 分组(没有时省略)
secret: graylog.token               # 凭据在 secrets.json 中的条目名(没有时省略)
secretRequired: true                # 没有凭据时报错；省略为 false(不带认证)
optionsSchema:                      # 合并后的参数按它校验(JSON Schema)
  type: object
  required: [url, streams]
  additionalProperties: false
  properties:
    url: {description: Graylog 的地址；写在 sites.json, type: string, pattern: '^https?://'}
    streams: {description: 只读这些流；为空时不限, type: array, items: {type: string}}
limitations:                        # 已知的限制，写给接入的人看
  - 每页最多 1000 条，超过时分页读取
example: |                          # 可选：配置示例
  "overrides": {"controls": {"collect.platform_errors": {"graylog": {"streams": ["app"]}}}}
```

参数的来源(按顺序合并，后者覆盖前者)：`sites.json` 中 `site` 分组 → settings 中该来源控制键下以方法名为键的一节(如 `controls."collect.platform_errors".graylog`) → 调用方给的额外参数。合并后按 `optionsSchema` 校验，一次列出全部问题；凭据另由 `secret` 取，只给程序放进请求头。

## 3. 写程序 `<方法>.py`

- 模块文档字符串写清：调用平台的哪个接口、怎么翻页、怎么判断读满、时间怎么换算、有哪些平台行为要注意(注明版本)；
- 只读：只发读请求；HTTP 经 `protocol/http.py`(`Platform`、`auth_headers`)，令牌只放进请求头，不进输出、日志与错误信息；
- 出错只抛带类型的错误(`collect/common/source.py` 的 `SourceUnavailable`、`SourceInvalid`、`SourceMisconfigured` 等)，不自己重试、不吞掉(重试只在 `protocol/limits.py`)；
- 读满条数上限时如实返回「读满」与已读到的最后时间，调用方据此让读取位置停在那里，下次接着读；
- 不在程序里写取值(地址、阈值)：都从 `configured.options` 取。

## 4. 缺省参数与测试

- 方法有缺省参数的，加进 `settings/defaults.json` 中该来源控制键下以方法名为键的一节；地址类的不写缺省，在 `settings/sites.example.json` 里给一个假值示例；
- 测试放在 `tests/` 下同样的路径(`tests/collect/platform_errors/log_platform/test_graylog.py`)，用注入的 `Transport` 回放录制的响应，不联网；
- 重新生成参考文档：`.venv/bin/python -m tightrein.cli.reference`，[平台方法](../reference/methods.md) 里就有了新方法。

## 5. 接入时选用

项目在 `setup.json` 中把模块的 `method` 写成新方法名，在 `sites.json`、`secrets.json` 填地址与凭据，`tightrein project check <模块>` 试跑。方法名写错时试跑报「没有这个方法」。

## 不是这种扩展的

- 需要按项目写具体内容(脚本、规则)的，写成方法文档由项目照着落地，不写成程序：项目探针见 [写项目探针](write-project-probe.md)，访问日志的项目数据源见 `src/tightrein/collect/access_log/project_sources/`；
- 部署来源(`release.deploy` 的 `github_actions`、`github_deployments`、`vercel`)目前写在 `src/tightrein/release/deploy.py` 里，不是方法清单，加新部署平台要改这个文件；
- 静态巡检的 Semgrep(`collect/static/tools/semgrep.yaml`)只有一个固定的工具，项目与技术栈自带的确定性工具按 `collect/static/tools/extension.schema.json` 的输出格式接入，见 `src/tightrein/collect/static/README.md`。
