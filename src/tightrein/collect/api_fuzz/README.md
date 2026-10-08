# API 模糊测试(collect.api_fuzz)

## 是什么

按接口描述(OpenAPI 或 Swagger)用 Schemathesis 自动构造请求打运行中的服务，**只查服务端报错(5xx)**。黑盒测试，不读代码，不调用模型。

需要：接口描述(仓库或工作区里的文件，或服务导出的 URL)、被测服务地址；测生产环境时还要允许清单。没有接口描述的项目不启用这个模块(接入清单写 disabled)。

## 流程

`source.py` 的 `collect(runtime)`：

1. 没有目标地址(`sites.json` 的 `target.baseUrl`，与项目探针等来源共用)：skipped；
2. 生产环境限制(`limits.py`)：`target.environment` 为 `production` 时只能测 GET、必须给允许清单，违反即报错，一个请求都不发；
3. Schemathesis 版本核对(锁定 4.28.0，用当前虚拟环境里的命令)；
4. 取接口描述(`spec.py` + `spec_source/` 的方法)：仓库文件按部署 commit 读并缓存；文件不存在时 skipped；
5. 没有新部署、接口描述也没变(state 表 `collect.api_fuzz` 的部署 commit 与接口描述哈希)：skipped；
6. 健康检查(`api_fuzz.health`)不通过：skipped，不调用 Schemathesis；
7. 登录(`login.py`)：取得的凭证立即登记进脱敏器；没有 `login` 时匿名运行；
8. 生成 `schemathesis.toml`(`schemathesis/config_writer.py`)，调用 Schemathesis(`schemathesis/invoke.py`)；
9. 把本次用过的 token 在报告目录的所有文件中按字节替换掉；
10. 解析 NDJSON 报告(`schemathesis/report_parser.py`)，转成信号(`mapping.py`)。

`replay.py` 的 `replay(runtime, evidence)` 供去重的复现确认调用：重放信号记录的请求，返回 `reproduced`、`not_reproduced` 或 `unavailable`。

## 输入与输出

| 输入 | 位置 |
|---|---|
| 选哪种接口描述 | 接入清单 `setup.json` 的 `collect.api_fuzz.method`：`openapi_file` 或 `openapi_url` |
| 接口描述的参数 | 工作区 `settings.json` 的 `overrides.controls."collect.api_fuzz".<方法>`(按 `spec_source/<方法>.yaml` 校验) |
| 被测地址与环境 | 工作区 `sites.json` 的 `target`：`baseUrl`、`environment`(各来源共用) |
| 健康检查、允许清单、登录方式 | 工作区 `sites.json` 的 `api_fuzz`：`health`、`allow`、`login` |
| 账号与密码 | 工作区 `secrets.json`：`api_fuzz.account`、`api_fuzz.password`(static-header 时 password 就是凭证) |

输出 `SourceResult`：信号(check_type `server_error`，location `方法 路由模板`，deterministic)，state(本次测过的部署与接口描述哈希、测到的接口)，原始输出在运行目录的 `15-collect.api_fuzz-raw/`(`schemathesis.toml`、`report/` 下的 NDJSON、VCR、JUnit 与日志、`openapi.json`、`refs/`)。

`sites.json` 示例：

```json
{"target": {"baseUrl": "https://staging.example.com", "environment": "staging"},
 "api_fuzz": {"health": "/healthz",
              "login": {"kind": "token-endpoint", "endpoint": "/api/auth/login",
                        "bodyTemplate": {"user": "{account}", "password": "{password}"}, "tokenPath": "data.token"}}}
```

## 配置

`settings/defaults.json` 的 `controls."collect.api_fuzz"`：`sourceTimeout`(整个来源的时限，与模型调用的 `timeout` 分开)、`maxExamples`、`phases`、`includeMethods`(空为不限)、`exclude`(排除路由的正则，合并为一条)、`workers`、`fuzzTimeout`(一次 Schemathesis 的时限)、`sanitizeKeys`(追加的脱敏键)、`reportErrorChars`、`healthTimeout`、`openapi_url.timeoutSeconds`。登录与重放的 HTTP 时限取 `limits.timeouts.http`。

## 设计依据

| 做法 | 为什么 | 出处 |
|---|---|---|
| 只查 5xx | 几乎没有误报，发现的都是真缺陷；「响应不符合描述」类检查在描述不准时大量误报 | 44 号计划 1.5 |
| 凭证只经环境变量 `TIGHTREIN_TOKEN` 传入，配置文件写 `Bearer ${TIGHTREIN_TOKEN}` 由 Schemathesis 展开 | 凭证不落盘、不进命令行 | 旧 config_writer、invoke |
| `keys-to-sanitize` 写「缺省清单 + 追加项 + 凭证请求头名」 | Schemathesis 4.28.0 给出这个键就替换缺省清单而不是追加 | 实测(旧计划 09) |
| 报告中 token 按字节替换 | 实测 Schemathesis 只对交互记录脱敏，NDJSON 的用例记录保留原始请求头 | 实测(旧计划 09) |
| `--config-file` 写在 `run` 之前 | Schemathesis 4 中它是顶层选项，写在后面被当成 run 的未知参数 | 实测 |
| 退出码 0、1 正常，2 为配置或描述错误，其余与超时为失败；失败的调用不产信号、不计覆盖 | 1 表示有失败用例；不完整的结果会让去重误判「已解决」 | 旧 invoke |
| 只解析 NDJSON，EngineFinished 出现才算完整；失败所属操作优先取 failure_info 的 operation | ScenarioFinished 的 recorder 已含完整请求与响应；有状态测试一个场景跨多个操作 | 实测 4.28.0 报告 |
| 健康检查不过整次跳过 | 环境宕机时会刷出一批 5xx | 旧 preconditions |
| 没有新部署且描述没变时跳过 | 同一次部署只测一遍，省时间 | 44 号计划 1.5「运行时机」 |
| 同一操作只留一条，计数记 sameCaseCount；message 不带响应内容 | 一个缺陷不刷几十条信号；消息稳定，去重才能归并 | 旧 mapping |
| 重放只看状态码，拿不到结论时为 unavailable | 「无法判断」不能当成「未复现」，否则会把真问题标成间歇 | 旧 replay |

## 不做什么

- 不做越权检查(角色能力对照、钩子、按角色多跑)、不做「响应不符合接口描述」类检查、不让 AI 起草接口描述、不分浅跑深跑；
- 不启动被测服务；不在生产环境发非 GET 请求；
- 不判断问题是否成立(交给去重与评估)。
