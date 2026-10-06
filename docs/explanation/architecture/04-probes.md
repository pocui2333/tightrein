# 能力层：sources

`sources` 负责各采集方法的具体执行：调用通用的外部工具、经扩展点读取平台与项目的数据、把结果转换成统一格式的信号。本篇覆盖
内部错误(`platform_errors`)、访问日志(`access_log`)、业务告警(`alerts`)、项目探针(`project_probes`)、`api_fuzz`、`static`、
`incidental` 七种方法，以及供 `verify` 复用的复现检查与页面检查的执行部分。取舍与原则见 `redesign/01-collect.md`(当前设计)，
编号、枚举、实体、表与路径沿用 `01-foundation.md`。

采集只从外部只读观察被测系统：被测系统内部的错误一律经它已接入的平台的只读查询 API 取得，不登录服务器、不读服务器上的
文件；采集中不跑页面用例(Playwright 只用于验证环节，第 3 节)。每种方法默认不启用，配置了才启用(`sources/enabled.py`)。

方法中不含任何项目名、技术栈名、框架特有的文件格式或类名(00 篇 3.6)：接口描述的导出、端点与角色的权限数据、平台的读取、
日志的解析、构建告警与依赖漏洞检查都经扩展点取得，扩展点的机制、输入输出与核心默认行为见 `10-extensions.md`，方法目录见
`docs/reference/methods.md`。本篇中以示例项目为例的内容一律标明「示例项目中的取值」。

## 1. 总体

### 1.1 职责与边界

| 负责 | 不负责 |
|---|---|
| 准备并调用通用的外部工具(Schemathesis、Semgrep)，经扩展点读取平台，运行项目探针 | 选择运行哪个方法、何时运行(`collect` 与编排层) |
| 经 `extensions` 调用各扩展点，处理扩展缺失与失败时的降级(10 篇第 4 章) | 技术栈与项目专属的解析与工具调用(由扩展实现) |
| 解析工具输出，产出 `Signal` 实体与运行覆盖范围 | 写数据库与交接文档(`collect` 经 `store` 写入) |
| 采集端脱敏(1.4) | 规范化、指纹、去重(`aggregate`) |
| 提供复现检查复用的执行部分(重放、Semgrep；执行器在 `pipeline/checks/regressions/`，第 7 节) | 判断回归是否关闭或重开 Issue(`aggregate`、`verify`) |
| 静态巡检中组织「确定性扫描 → 审查 → 取证」的顺序 | 组装 agent 任务的说明与上下文(`pipeline/collect/prompts`，经注入的审查器接口提供) |

采集方法只依赖基础层与同层的 `runner`、`guards`、`vcs`(只读)、`retrieval`、`extensions`；不依赖任何流水线模块。静态巡检需要的 agent 调用通过调用方注入的 `Reviewer` 接口完成，探针内部不含提示正文。

### 1.2 文件划分

```
sources/
  base.py                    Probe 协议、ProbeTarget、ProbeOptions、ProbeOutcome、save_state
  enabled.py                 各方法是否启用(配置了才启用)与未启用的原因
  common/
    target.py                目标版本：最近一次成功部署、某个时间点之前的部署
    window.py                平台来源按时间窗口增量读取、读取位置、保留期缺口
    session.py               按角色登录换取凭证；只在内存中持有
    signals.py               Signal 的构造：编号生成、字段校验、截断
    redact.py                采集端脱敏：请求头、查询参数、响应摘录、复现命令
    raw.py                   原始输出目录的创建与登记(data/runs/<运行编号>/raw/<方法>/)
    procs.py                 外部进程的启动、超时终止、退出码与输出的捕获
    routes.py                按接口描述把实际 URL 匹配为接口路由模板
  platform_errors/
    source.py                PlatformErrorsSource：错误追踪平台与集中日志平台各自的窗口与读取位置
    tracking.py              错误追踪平台的分组到 Signal(平台分组编号为指纹)
    logs.py                  日志条目到 Signal
    select.py                按级别筛选日志条目，截取本项目帧
  access_log/
    source.py                AccessLogSource：经日志平台取访问日志，与基线比较
    parse.py                 访问日志行的字段映射(JSON 字段或正则命名分组)
    stats.py                 按接口统计、与基线比较、基线的指数平均
  alerts/
    source.py                AlertsSource：读取已触发的业务告警，排除基础设施类
  project_probes/
    source.py                ProjectProbeSource：运行到期的项目探针；trial 供 probe test
    registry.py              登记(sources.project-probes)与到期判断
    runner.py                子进程收发 JSON、契约校验、信号构造
    helpers.py               供探针复用：读写 JSON、钥匙串、脱敏、经日志平台取数
  api_fuzz/
    probe.py                 ApiFuzzProbe
    spec.py                  经 spec-export 取得 data/specs/<commit>/openapi.json，按 commit 复用
    checks.py                检查项的分级与开关(sources.api-fuzz.checks)
    limits.py                按环境限制可测的请求(生产环境只测 GET 与允许清单)
    authz/
      model.py               经 authz-endpoints 与 authz-roles 取得权限数据，合成越权检查的数据
    config_writer.py         为每次运行生成 schemathesis.toml
    invoke.py                按角色调用 Schemathesis CLI
    hooks.py                 自定义检查(@schemathesis.check)
    report_parser.py         解析 NDJSON 事件流与 VCR 录制
    mapping.py               失败条目到 Signal 的映射与过滤
    replay.py                重放一条已记录的请求(复现确认、复现检查)
  static/
    probe.py                 StaticProbe：审查、取证与待处理清单
    scope.py                 确定扫描范围：上次巡检的 commit 到本次 commit 的 diff
    baseline.py              基线审查的文件筛选与按目录模块分批(小模块合批)
    tools/
      extension.py           经 static-tools 取得确定性工具的结果，转为 ToolFinding
      semgrep.py             Semgrep 的执行与解析
    reviewer.py              Reviewer 协议与候选主张、取证结果的数据类
    mapping.py               取证成立的主张到 Signal 的映射
  incidental/
    probe.py                 IncidentalProbe
    handoff_source.py        从分诊与修复的交接文档读取任务外发现
    archive_import.py        从迁移归档的 markdown 报告一次性导入
    locate.py                从发现原文中提取 文件:类名.方法名
    mapping.py               发现到 Signal 的映射
```

复现检查的执行器在 `pipeline/checks/regressions/`(第 7 节)，供采集、修复与验证共用，复用本组件各探针的执行部分：

```
pipeline/checks/regressions/
  manifest.py                复现检查清单的读取与校验
  api_check.py               API 类：复用 api_fuzz.replay
  page_check.py              页面类：经 pipeline/checks/pages 的页面运行器(第 3 节)
  static_check.py            静态类：复用 static.tools.semgrep
  repo_test_check.py         测试类：在 worktree 中运行项目测试命令选中的一个测试
  runner.py                  按 Issue 与类型执行复现检查，返回结果
```

### 1.3 统一接口

每个探针实现同一个协议：

```
class Probe(Protocol):
    name: Probe
    levels: tuple[ProbeLevel, ...]
    def run(self, target: ProbeTarget, level: ProbeLevel, options: ProbeOptions) -> ProbeOutcome
```

`options` 携带各方法自己的参数(1.4)，缺省值来自 `project.yaml` 的 `sources` 段，命令行参数覆盖配置。

| `ProbeTarget` 字段 | 说明 |
|---|---|
| `environment` | `staging`、`production` 或 `local`(verify 在本机启动的服务) |
| `base_url` | 目标地址；`--target` 覆盖 |
| `release` | 目标版本的 commit：api-fuzz 取 `deployments` 表中最近一次成功部署；static 取 `origin/<主分支>`；平台来源与项目探针的每条信号另按发生时间取当时的部署；`--commit` 覆盖 |
| `worktree` | 只读 worktree 的路径 |
| `run_id` | 本次运行编号，写入每条信号的 `run_id` |
| `raw_dir` | 本方法的原始输出目录 |
| `clock` | `Clock` 实例，`--now` 可替换 |

| `ProbeOutcome` 字段 | 说明 |
|---|---|
| `signals` | 本次产出的 `Signal` 列表 |
| `coverage` | `Coverage`：接口与角色、方法范围、文件，以及本次读到数据的来源(`sources`：error-tracking、log-platform、access-log、alert-source 或项目探针名) |
| `environment` | 环境事实：健康检查结果、登录失败的角色、报告是否完整 |
| `status` | `ok`、`partial`(部分角色或部分工具失败)、`failed`、`skipped`(无需运行，例如没有新提交) |
| `stats` | 计数：测试的接口数、失败数、读到的分组与日志条数、证据不足的主张、待处理疑点等，供运行摘要使用 |
| `artifacts` | 原始输出文件的相对路径清单 |
| `notes` | 需要在运行摘要中说明的事项，例如保留期缺口 |
| `cursors`、`probe_states`、`pending_claims`、`sources` | 平台来源的读取位置、项目探针的状态、静态巡检的待处理疑点、incidental 的已读记录；由 `collect` 在写入信号的同一事务中经 `save_state` 保存 |

档位 `ProbeLevel`：

| 探针 | 可用档位 | 默认 |
|---|---|---|
| api-fuzz | `shallow`、`deep` | `shallow` |
| static | `incremental`、`full`(包含 `incremental`)、`baseline`(基线审查；没有上次巡检终点的 `full` 也按它运行) | `incremental` |
| platform-errors、access-log、alerts、project-probe、incidental | 无档位，传入值被忽略 | — |

### 1.4 各探针的参数

| 探针 | `ProbeOptions` 参数 | 用途 |
|---|---|---|
| api-fuzz | `roles`、`include_paths`、`exclude_path_regex`、`max_examples`、`phases`、`include_methods`、`max_response_time`、`from_raw` | `verify` 以 `include_paths` 限定到改动涉及的接口；`from_raw` 只重新解析已有报告，不调用工具 |
| static | `base_commit`、`reviewer`、`max_claims`、`pending`、`queued` | `reviewer` 由 `collect` 注入(5.4)；`base_commit` 缺省为上一次成功的 static 运行的 `target_commit`；`queued` 为要取证的待处理疑点(5.2)，`pending` 为 `--select pending:<编号>\|low` 的选择 |
| project-probe | `names` | `--select name:<名称>` 只运行指定的探针(不看间隔) |
| incidental | `import_archive` | 迁移时指定归档目录，一次性导入历史报告 |

### 1.5 信号字段的公共约定

统一信号格式(1.3)与 `Signal` 实体的对应：

| 1.3 字段 | 实体字段 | 公共约定 |
|---|---|---|
| `signalId` | `id` | `S-<ULID>`，由 `common/signals.py` 按 `clock` 生成 |
| — | `run_id` | 取 `target.run_id` |
| `source` | `source` | 探针决定，见各节 |
| — | `probe`、`check` | 实体的独立字段，不再放进 `context` |
| `environment` | `environment` | 取 `target.environment` |
| `occurredAt` | `occurred_at` | UTC，精确到秒 |
| `release` | `release` | 见各节 |
| `location` | `location` | 路由模板、页面路由或 `文件:类名.方法名`；规范化由 `aggregate` 完成 |
| `message` | `message` | 原文经脱敏，截断到 1000 字符 |
| `context` | `context` | 各探针特有字段，见各节；单条信号的 `context` 序列化后不超过 16 KB，超出部分写入 `raw/` 并以 `*Ref` 字段引用 |
| `actor` | `actor` | `{ "id": <账号别名>, "role": <角色> }`；账号别名取 `project.yaml` 中的角色键，不是真实用户名 |
| — | `normalized_message`、`fingerprint` | 探针不填，由 `aggregate` 写入 |
| — | `suppressed` | 探针固定写 `false` |

采集端脱敏由 `common/redact.py` 执行，规则与 `observability/redact.py` 共用一份，额外处理：请求头中的 `Authorization`、`Cookie`；复现命令中的 token 替换为 `<TOKEN>`；响应摘录截断到 500 字符。

### 1.6 凭证的处理

| 场合 | 做法 |
|---|---|
| 测试账号密码 | `config/secrets.py` 按条目名从钥匙串读取，只在内存中传递 |
| API 凭证 | `common/session.py` 按 `accounts.login.kind` 取得：`token-endpoint`(缺省)调用 `accounts.login` 指定的接口，按 `tokenPath` 从响应中取出 token，请求时放进 `Authorization: Bearer <token>`；`static-header` 时钥匙串条目中的密码就是凭证，原样放进 `accounts.login.header` 指定的请求头，账号名取角色名，配置了 `verify` 时先请求该接口校验(2xx 即有效)。取得的凭证立即登记到脱敏器 |
| 匿名身份 | 保留角色名 `anonymous`：不读钥匙串、不请求登录接口，请求不带凭证请求头。没有 `accounts` 时 api-fuzz 与验证的页面检查以匿名身份运行(`session.probe_roles`)，也可以用 `--select role:anonymous` 显式选择；信号的 `actor` 与覆盖范围的角色记为 `anonymous`。匿名身份不传给 `authz-roles`，越权模型中没有它，它的 401、403 状态码不符计入 `stats.unjudgedAuthFailures`，越权检查对它不报 |
| 传给 Schemathesis | 经子进程环境变量 `TIGHTREIN_TOKEN` 传入，`schemathesis.toml` 用 `${TIGHTREIN_TOKEN}` 引用；token 不出现在命令行参数中 |
| 平台的只读令牌 | 方法进程内经 `config/secrets.Keychain` 按 `keychainItem` 读取，只放进请求头，不写进输出、日志与错误信息(扩展进程的环境变量会滤掉凭证，因此不经环境变量传入) |
| 项目探针的凭证 | 探针经 `helpers.secret` 读取，只允许读 `sources.project-probes[].keychain` 登记的条目；读到的值登记到脱敏器 |
| 原始输出 | Schemathesis 报告开启输出脱敏(2.4)；验证环节的 Playwright trace 由 `pipeline/checks/pages/artifacts.py` 清除凭证(第 3 节) |

## 2. api_fuzz

### 2.1 职责

经 `spec-export` 扩展从只读 worktree 取得接口描述，按角色用 Schemathesis 对目标环境运行模糊测试，把失败转成信号；有权限数据时提供越权自定义检查；提供单条请求的重放，供复现确认与复现检查复用。

### 2.2 运行步骤

| 步骤 | 函数 | 说明 |
|---|---|---|
| 1 | `spec.ensure(release)` | `data/specs/<commit>/openapi.json` 已存在且缓存键一致则复用；否则要求只读 worktree 的 HEAD 等于 `release`，调用 `spec-export`(10 篇 3.1)。接口描述不来自仓库时(`core/openapi-file` 的 `base: workspace`)不需要只读 worktree，`repo` 为空 |
| 2 | `authz.model.ensure(release)` | 同上，调用 `authz-endpoints` 与 `authz-roles`(10 篇 3.2、3.3)，合成 `data/specs/<commit>/authz-model.json`；两者任一没有实现或失败时本次不做越权检查 |
| 3 | `session.login_all(roles)` | 逐个角色登录；失败的角色记入 `outcome.environment.failedRoles`，不参与后续步骤 |
| 4 | `config_writer.write(level, options)` | 生成 `raw/api-fuzz/schemathesis.toml` |
| 5 | `invoke.run_role(role)` | 每个登录成功的角色一次 Schemathesis 调用，报告写入 `raw/api-fuzz/<角色>/` |
| 6 | `report_parser.parse(role_dir)` | 得到失败条目与已测试的操作 |
| 7 | `mapping.to_signals(failures)` | 过滤与映射，得到信号与覆盖范围 |

**接口描述**(1.6.1)：`spec.py` 以只读 worktree 为 `repo` 调用 `spec-export`(实现不读仓库时 `repo` 为空，由 `ExtensionClient.needs_repo` 判断)，扩展把接口描述写入 `data/specs/<commit>/openapi.json` 并返回格式与操作数；核心检查文件可解析且含 `paths`。构建与导出所需的一切(构建命令、导出工具、启动期需要的假密钥)由扩展负责，核心不知道其内容。调用发生在任何 agent 运行之前，此时 worktree 尚未被 `guards` 设为不可写。示例项目中的取值：由技术栈扩展 `aspnetcore` 构建后端并导出(10 篇 5.3)。

### 2.3 越权检查的数据来源

越权检查需要两份数据，都经扩展点取得，合成一个 `authz-model.json`：

| 数据 | 扩展点 | 输出要点 |
|---|---|---|
| 端点 → 所需能力 | `authz-endpoints`(10 篇 3.2) | 每个端点的方法、路由模板、所需能力列表、是否匿名、处理方法与源文件 |
| 角色 → 能力 | `authz-roles`(10 篇 3.3) | 每个测试角色对每个能力是否具备 |

- 两份扩展输出由 `contracts/schemas/extension/points/` 中的 schema 校验，`model.py` 合并后按 `contracts/schemas/data/authz-model.schema.json` 写出。
- 路由模板的写法与 `openapi.json` 中的路径保持一致，合并时按「方法 + 路由模板」对齐；对不上的端点、`requires` 中出现而角色数据中没有的能力、角色数据中缺少的角色，列入 `outcome.notes`，不参与越权检查。
- 没有两份数据中的任一份时，按 10 篇 4.1 处理：不注册越权检查，`status_code_conformance` 的 401、403 失败不产出信号，计入 `stats.unjudgedAuthFailures`。
- 示例项目中的取值：端点数据由技术栈扩展 `aspnetcore` 从构建产物读取 `[Authorize(Policy)]` 等声明(10 篇 5.4)，角色数据由示例项目的项目扩展解析角色能力矩阵(10 篇 6.2)。

### 2.4 Schemathesis 的调用

Schemathesis 作为核心 Python 环境的依赖安装，版本锁定；以子进程调用其 CLI，每个角色一次：

```
schemathesis run <openapi.json>
  --config-file <raw/api-fuzz/schemathesis.toml>
  --url <base_url>
  --max-examples <N>
  --phases <阶段列表>
  [--include-method GET]
  [--include-path <路径> ...]
  --exclude-path-regex <排除正则>
  [--max-response-time <秒>]
  --report junit,vcr,ndjson
  --report-dir <raw/api-fuzz/<角色>/>
  [--seed <整数>]
```

| 参数 | 浅跑 | 深跑 |
|---|---|---|
| `--max-examples` | 50 | 200 |
| `--phases` | `examples,coverage,fuzzing` | `examples,coverage,fuzzing,stateful` |
| `--include-method` | `GET` | 不限定 |
| `--exclude-path-regex` | `sources.api-fuzz.exclude` 中各条正则以 `\|` 合并为一条 | 同左 |
| `--max-response-time` | `thresholds.slowResponseSeconds`(只在响应过慢检查开启时) | 同左 |
| `--exclude-checks` | 关闭的检查项 | 同左 |
| `--seed` | 每次运行随机生成并记入 `outcome.stats`，复查时可用同一种子重跑 | 同左 |

`schemathesis.toml` 由 `config_writer.py` 生成，内容只含非敏感值：

| 键 | 取值 |
|---|---|
| `headers` | `token-endpoint` 为 `{ Authorization = "Bearer ${TIGHTREIN_TOKEN}" }`，`static-header` 为 `{ "<accounts.login.header>" = "${TIGHTREIN_TOKEN}" }`；匿名身份使用另一份 `schemathesis-anonymous.toml`，没有 `headers` |
| `hooks` | `"tightrein.sources.api_fuzz.hooks"` |
| `workers` | `sources.api-fuzz.workers` |
| `[output.sanitization]` | 保持开启；`keys-to-sanitize` 在默认值之外加入 `sources.api-fuzz.sanitizeKeys` 与凭证请求头名 |

- 检查项按价值分级(`checks.py`，`sources.api-fuzz.checks`)：服务器报错、越权、状态码不符缺省开启；响应结构不符为 `auto`，接口描述由框架导出(spec-export 的方法不是手写文件的 `core/openapi-file`)时开启、手写时关闭；响应过慢、不支持的方法缺省关闭。关闭的检查以 `--exclude-checks` 传入；越权关闭时不加载越权模型；响应过慢开启时才传 `--max-response-time`。
- 按环境限制(`limits.py`)：`target.environment` 为 `production` 时只测 GET，且只测 `sources.api-fuzz.production.allow` 列出的路由；各档位的 `includeMethod` 不是 GET 或允许清单为空时启动即报错(`ConfigError`，写明键名)，不发出任何请求。
- 接口描述没有自动导出时，`tightrein project spec draft` 以只读任务 `spec-drafter` 读代码起草 `openapi.draft.yaml`，用户在接入清单的「接口描述」项确认后按 `core/openapi-file`(`base: workspace`)登记。
- 退出码：`0` 全部通过，`1` 有检查失败，两者都视为正常完成；`2` 为配置或接口描述错误，该角色记为失败。

### 2.5 报告格式与解析

| 报告 | 用途 |
|---|---|
| NDJSON 事件流 | 解析的主来源：逐条读取引擎事件，从场景结束事件中取出每个操作的检查结果与失败详情，从中得到已测试的操作集合；以引擎结束事件是否出现判断报告是否完整 |
| VCR 录制 | 按失败用例取完整的请求与响应，填充 `context.request` 与 `context.response` |
| JUnit | 不解析，保留在 `raw/` 供人查看 |

- 事件与录制的字段取法写在 `report_parser.py` 中，以锁定版本实际产出的报告为测试夹具(2.10)。升级 Schemathesis 时先更新夹具，解析测试通过后才升级锁定版本。
- 报告不完整(没有引擎结束事件)时，该角色记为失败，原因为「报告不完整」，不按「零失败」处理。

### 2.6 失败的过滤与映射

**过滤**(`mapping.py`)：

| 情况 | 处理 |
|---|---|
| `status_code_conformance` 失败，状态码为 401 或 403，且 `authz-model.json` 表明该角色本就不具备该端点所需能力 | 丢弃：这是预期的拒绝 |
| `status_code_conformance` 失败，状态码为 401 或 403，本次没有 `authz-model.json`，或模型中没有该端点或该角色(包括匿名身份) | 无法判断是否为预期的拒绝，丢弃并计入 `stats.unjudgedAuthFailures` |
| 同一角色、同一操作、同一检查的多个失败用例 | 只保留一条信号，其余用例的数量记入 `context.sameCaseCount` |

**信号字段**

| 字段 | 取值 |
|---|---|
| `source` | `synthetic` |
| `check` | Schemathesis 的检查名；越权为 `unauthorized_role_access` |
| `location` | `<方法> <路由模板>`，直接取接口描述中的路径 |
| `message` | 失败的标题，例如「服务端返回 500」 |
| `occurred_at` | 失败用例对应交互的记录时间；取不到时用该角色调用的结束时间 |
| `release` | `target.release` |
| `actor` | 该角色的账号别名与角色 |

| `context` 字段 | 说明 |
|---|---|
| `role` | 发起请求的角色 |
| `request` | `method`、`path`(实际路径)、`pathTemplate`、`query`、`body`；body 超过 16 KB 时写入 `raw/` 并以 `bodyRef` 引用 |
| `response` | `status`、`elapsedMs`、`bodyExcerpt` |
| `reproduce` | curl 形式的复现命令，凭证请求头的值为 `<TOKEN>`；匿名身份不带凭证请求头 |
| `seed` | 本次运行的随机种子 |
| `sameCaseCount` | 同类失败用例数 |
| `requiredCapabilities`、`grantedCapabilities` | 仅越权检查：端点要求的能力与该角色具备的能力 |
| `reportPath` | 该角色报告目录的相对路径 |

**覆盖范围**：`coverage.endpoints` 为每个已测试操作的「方法 + 路由模板 + 角色」；`coverage.endpointsTotal` 为接口描述中扣除排除范围后的接口总数，供 `learn` 计算接口覆盖率；`coverage.methods` 浅跑为 `GET`，深跑为 `all`。只计入报告完整的角色。

### 2.7 越权自定义检查

`hooks.py` 用 `@schemathesis.check` 注册 `unauthorized_role_access(ctx, response, case)`：

1. 模块加载时读取环境变量 `TIGHTREIN_AUTHZ_MODEL`(模型文件路径)与 `TIGHTREIN_ROLE`(当前角色)，两者由 `invoke.py` 设置；本次没有模型时 `invoke.py` 不设置这两个变量，检查不注册。
2. 从 `case` 取得方法与路由模板，在模型中查找该端点所需的能力；匿名端点、不需要能力的端点、模型中找不到的端点直接通过。
3. 当前角色缺少任一所需能力，而响应状态码为 2xx 时，抛出 `AssertionError`，消息写明角色、端点与缺少的能力。

这项检查只覆盖端点级别的准入；方法体内按数据归属收窄的校验不在其范围内(1.6.1)。

### 2.8 请求重放

`replay.py` 对外提供：

```
replay(request: RecordedRequest, role: str, target: ProbeTarget, session: Session) -> ReplayResult
```

- `RecordedRequest` 取自信号的 `context.request`(或 `bodyRef` 指向的文件)，用该角色的新凭证(`Session.headers`)重新发送；匿名身份不带凭证。
- `ReplayResult` 包括状态码、耗时、响应摘录，以及按原信号的 `check` 重新判定的结果(5xx、越权、超时等判定逻辑与 `mapping.py` 共用)。
- 使用者：`aggregate` 的复现确认(2.7 重放 2 次)、`regressions/api_check.py`、`verify`。

### 2.9 错误处理

| 情况 | 处理 |
|---|---|
| 没有目标地址(没有 `target.baseUrl` 也没有 `--target`) | `skipped`，原因写明缺少目标地址 |
| 没有 `accounts` | 以匿名身份运行(1.6)，只做匿名身份时不做越权检查，`notes` 写明 |
| 接口描述来自仓库，而只读 worktree 不存在或不在 `release` 且没有缓存的接口描述 | 探针不运行，`status` 为 `failed`，提示「先执行 `tightrein project worktree sync --commit <release>`」 |
| 没有 `spec-export` 的实现 | `skipped`，原因「未提供 spec-export 扩展」 |
| `spec-export` 返回错误(`build-failed`、`tool-missing` 等)或超时 | `failed`；扩展的 `message`、`hint` 写入 `notes`，标准错误与构建输出保存到 `raw/api-fuzz/spec-export.log` |
| `authz-endpoints` 或 `authz-roles` 返回错误 | 本次不做越权检查，其余照常，`status` 为 `partial`，原因写入 `notes` |
| 某角色登录失败 | 跳过该角色，`status` 为 `partial`，记入 `environment.failedRoles` |
| 全部角色登录失败 | `failed` |
| Schemathesis 未安装或版本不符 | `failed`，提示重新安装核心依赖 |
| 退出码 2、报告不完整、超过 `sources.api-fuzz.timeoutMinutes` | 该角色失败；超时由 `common/procs.py` 终止进程 |
| 解析时遇到未知的事件或字段 | 该条目跳过并计数，计数大于 0 时写入 `notes` |

### 2.10 测试

| 对象 | 方式 |
|---|---|
| `report_parser.py` | 以锁定版本实际产出的 NDJSON 与 VCR 为夹具：含 5xx、契约不符、超时、越权失败、报告截断五类 |
| `mapping.py` | 过滤规则与字段映射的单元测试，含 401/403 预期拒绝的丢弃 |
| `hooks.py` | 构造 `case` 与 `response` 替身，覆盖匿名端点、缺能力返回 2xx、缺能力返回 403 |
| `spec.py` | 以假扩展(输出固定 JSON 的脚本)覆盖：缓存命中、缓存键变化后重新调用、HEAD 不在 `release`、扩展返回错误 |
| `authz/model.py` | 两份扩展输出的合并、对不上的端点、缺少的能力与角色、没有权限数据时不注册检查 |
| `config_writer.py` | 生成的 toml 不含任何 token，浅跑与深跑参数正确 |
| 端到端 | 集成测试在本机启动一个带 OpenAPI 描述的桩服务，运行真实的 Schemathesis；工具不存在时跳过 |

## 3. 页面检查(验证环节，pipeline/checks/pages/)

采集中不做页面测试(redesign/01-collect.md 第 2 节)。Playwright 只在验证环节运行：合并前验证的页面巡检与截图评审(工作区
`e2e/` 下的巡检用例)，以及页面类复现检查(复现检查目录作为 `regress-<角色>` 项目的 testDir)。`PageRunner.run(target, roles,
spec_dirs, grep)` 返回 `PageRun`：状态、页面上的失败(用例失败、控制台报错、失败请求，按发生的页面路径记录)、产物与说明。

| 文件 | 内容 |
|---|---|
| `plan.py` | 本次运行的计划文件：每个角色一个 setup 项目，巡检为 `patrol-<角色>`，复现检查为 `regress-<角色>`；参数取 `checks.pages`(locale、retries、timeoutMinutes、patrolGrep、ignoreRequests) |
| `invoke.py` | `npx playwright test --config playwright.config.ts --project ...`；子进程环境只含白名单变量、计划文件路径(`TIGHTREIN_PAGE_PLAN`)与各角色密码(`TIGHTREIN_PASSWORD_<角色>`) |
| `result_parser.py`、`failures.py` | 解析自定义 reporter 写出的 results.ndjson，整理页面上的失败 |
| `artifacts.py` | 改写 trace.zip，清除请求头、cookie 与本地存储中的凭证，无法改写的删除 |
| `runtime/` | Node 侧文件(Playwright 配置、登录、fixture、reporter)，Playwright 安装在此目录 |

登录态文件写入本次运行新建的临时目录(权限 0700)，运行结束后删除。没有被测地址时为 skipped；全部角色的账号都不可用、
没有任何用例得到执行或结果文件不完整为 failed；部分角色不可用、setup 失败或超时为 partial。

## 4. 平台来源与项目探针

### 4.1 内部错误(platform_errors)

只采集两种：应用运行报错(运行日志中带堆栈的错误与异常)与前端错误(浏览器端 SDK 采集的 JS 报错)。来源与读取：

| 来源 | 扩展点 | 读取 | 信号 |
|---|---|---|---|
| 错误追踪平台 | `error-tracking`(`core/sentry`) | 窗口内有新事件的错误分组，含最新事件的堆栈帧与操作轨迹 | 每个分组一条：check 为 `error`(后端)或 `frontend-error`，location 为第一条本项目帧「文件:函数」，没有时为 culprit；`context.platformGroup` 为平台分组编号(`sentry:<组织>/<编号>`)，直接作为指纹 |
| 集中日志平台 | `log-platform`(`core/loki`)加 `log-parse` | 按 `sources.platform-errors.logQuery` 查询窗口内的原文，经 `log-parse`(`core/json-lines`、`core/regex`)映射字段 | 级别属于 `sources.platform-errors.levels` 的条目各一条：location 为本项目帧或日志类别，指纹按逻辑位置计算(异常类型与前三帧，或类别与归一化消息) |

- **增量读取**(`common/window.py`)：每个来源一个读取位置(`source_cursors`，键为 `platform-errors:error-tracking`、`platform-errors:log-platform`)，窗口为上次的终点到现在，首次读取回看 `initialLookbackHours`。平台给出的最早可查时间晚于窗口起点时写「可能漏读」；日志读满 `logLimit` 时读取位置停在已读到的最后一条，其余下次继续。
- 两个来源都没有配置时为 skipped(未启用)；某个来源失败时不保存它的读取位置，另一个照常，状态为 partial；都失败为 failed。读到数据的来源记入 `coverage.sources`，覆盖运行据此判断平台问题是否已解决(05 篇 3.6)。
- 信号的 `release` 取发生时间之前最近一次成功部署的 commit，平台上的版本号记在 `context.platformRelease`。
- 取回的内容先脱敏再存储(信号构造时)。被测系统没有接入这类平台时不启用，运行时缺陷由 api-fuzz 与静态巡检发现。

### 4.2 访问日志(access_log，可选)

`sources.access-log.query` 有值且配置了 `log-platform` 时启用。经日志平台取窗口内的访问日志，按 `fields`(JSON 字段)或
`pattern`(正则命名分组 method、route、status、durationMs)解析为请求，按接口统计请求数、p95 耗时与 5xx 比例，与基线比较：
本次与基线的请求数都不少于 `minRequests` 时，p95 超过基线的 `latencyRatio` 倍为耗时退化，5xx 比例高出 `errorRateDelta` 为
错误比例退化，各产出一条信号(source 为 performance，check 为 `latency-regression` 或 `error-rate-regression`)。基线按
`baselineWeight` 指数平均，保存在读取位置的 `baseline` 中；第一次运行只建立基线。

### 4.3 业务告警(alerts)

经 `alert-source`(`core/alertmanager`，Alertmanager API v2，Prometheus、Grafana 内置告警、Grafana Cloud 通用)读取已触发、
未静默的告警，不重复实现告警规则。基础设施类按 `sources.alerts.exclude` 排除：告警名匹配 `names` 中的正则，或某个标签的取值
在 `labels` 给出的清单中(核心缺省覆盖节点、磁盘、内存、CPU、容器重启、Watchdog 等)。其余每条一条信号：source 为 behavior，
check 为 `business-alert`，location 为告警名，严重度提示取标签 `severity`，指纹为 `alertmanager:<fingerprint>`。

### 4.4 项目探针(project_probes)

被测系统没有业务监控时，项目在工作区编写自己的只读检查脚本(写法见 `docs/how-to/write-project-probe.md`，契约见
`docs/reference/project-probe.md`)。

| 环节 | 做法 |
|---|---|
| 登记 | `sources.project-probes`：name、command(相对工作区，`{python}` 为核心解释器)、every(`15m`、`1h`、`1d`)、keychain(只读凭证的条目名)、timeoutSeconds；`tightrein project probe new <名称>` 生成模板并按行写回登记 |
| 调度 | 编排的 `sources` 步骤按各探针的 `every` 运行到期的探针(`probe_states` 记上次运行时间)；`--select name:<名称>` 不看间隔 |
| 输入 | 标准输入一个 JSON：name、lastRunAt、state(上次输出的 state)、window(上次运行到现在)、workspace、environment、baseUrl |
| 输出 | 标准输出一个 JSON：signals(location、symptom、evidence、severityHint、fingerprint，可选 occurredAt、context)、state、notes；按 `data/project-probe-output.schema.json` 校验 |
| 失败 | 退出码非 0、超时、输出不是 JSON 或不合契约：本次作废，不产出信号、不保存状态，原因写进说明，标准错误脱敏后写入原始输出目录 |
| 信号 | source 为 behavior，check 为探针名，message 为 symptom；指纹为探针名与给出指纹的哈希；探针名记入 `coverage.sources` |
| 试跑 | `tightrein project probe test <名称>` 单独运行一次、校验输出、显示将产出的信号，不写数据库、不保存状态、不进入归并 |
| 辅助函数 | `helpers`：read_input、signal、emit(写前校验)、secret(只读登记的钥匙串条目)、redact、query_logs(经 `tightrein project probe logs` 调用工作区的日志平台方法) |

## 5. static

### 5.1 职责

在只读 worktree 上对新提交做静态巡检：先运行确定性工具(经 `static-tools` 扩展取得的技术栈工具结果，以及核心直接调用的 Semgrep)，再经注入的 `Reviewer` 由 agent 对照缺陷模式审查，最后逐条交给 `claim-verifier` 取证，只有判定成立的主张产出信号(1.6.4)。

### 5.2 运行步骤

| 步骤 | 函数 | 说明 |
|---|---|---|
| 1 | `scope.resolve(base_commit, head)` | `head` 为只读 worktree 的 HEAD；两者相同且档位为 `incremental` 时返回 `skipped`；`full` 档位没有 `base_commit`(第一次巡检)时按 `baseline` 运行 |
| 2 | `tools.extension.run(scope)`、`tools.semgrep.run(scope)` | 运行确定性工具与规则库(5.3)，结果只保留落在改动文件中的条目(依赖漏洞除外，见 5.3)；`full`、`baseline` 档位不限文件 |
| 3 | `guards.before(read_only_task)` | 把只读 worktree 设为不可写 |
| 4 | `reviewer.review(scope, tool_findings)` 或 `reviewer.review_baseline(batch, tool_findings)` | `incremental`、`full`：增量审查。`baseline`：`baseline.plan` 按目录模块分批(5.4)，逐批基线审查；确定性工具的结果分给所在文件的批，不在任何一批中的(如依赖清单)给第一批；某批报告当天预算用尽时其余批次不再运行 |
| 5 | `reviewer.scan_variants(pattern)` | `full`、`baseline` 档位：对每个缺陷模式做全仓库同类实例查找 |
| 6 | `reviewer.verify(claim)` | 低级疑点不在采集时取证，直接进入待处理清单(`pending_claims`，原因 `low`)；先取证清单中超出上限遗留的疑点(`queued`，原因 `over-limit`)，再按严重度(高、中、未标注)取证本次的疑点，合计不超过上限(`sources.static.maxClaims`，基线为 `baseline.maxClaims`)；超出上限与预算用尽后未取证的进入清单(`over-limit`)，后续运行继续处理。`--select pending:<编号>\|low` 时不审查，只取证选中的疑点。取证结论随信号交给分诊直接复用(06 篇) |
| 7 | `guards.after(...)` | 恢复可写，检查 worktree 与 git 状态未被改动 |
| 8 | `vcs.blame(file, line, head)` | 对取证成立的主张查引入的 commit |
| 9 | `mapping.to_signals(verified)`、`mapping.rule_signals(hits)` | 产出信号；规则库的命中直接成为信号 |

### 5.3 确定性工具

确定性工具分两部分：

| 部分 | 来源 | 说明 |
|---|---|---|
| 技术栈与项目的工具 | `static-tools` 扩展(10 篇 3.6) | 构建告警、依赖漏洞等与技术栈有关的检查；扩展负责执行命令与解析输出，返回统一的发现条目与每个工具的执行状态，原始输出写入 `raw/static/` |
| 代码规则 | 核心的 `tools/semgrep.py` | `semgrep scan --config <配置>... --json --metrics=off <文件…>`，配置取 `sources.static.semgrep.configs`；解析 `results` 中的 `check_id`、`path`、`start.line`、`extra.message`、`extra.severity`，以及 `errors` |
| 规则库 | 工作区 `rules/`(01 篇 4.3) | 缺陷变规则验证通过的 Semgrep 规则(design 8.6)，单独运行一次 Semgrep，原始输出写入 `raw/static/rule-library/`；命中不经审查与取证，直接映射为信号，`check` 为 `rule:<规则编号>`；运行失败时巡检为 `partial` |

- `tools/extension.py` 把扩展返回的每条 `findings` 转成 `ToolFinding`：工具、规则或代码、文件、行、消息、严重度；依赖漏洞以依赖清单文件为文件、包名与版本为定位。`incremental` 档位只保留 `file` 在改动文件中的条目，依赖漏洞不受此限制。
- Semgrep 退出码 0 表示正常完成(不加 `--error` 时有结果也返回 0)；其他退出码按失败处理。没有 `sources.static.semgrep.configs` 时不运行 Semgrep。
- Semgrep 的命令取本机用户配置 `tools.semgrep.path`，没有时取 `runtime.tools.semgrep`(缺省 `semgrep`，按 PATH 查找)；含 `/` 的相对路径相对本工具仓库根目录解析。组装根解析一次，static 探针、静态类复现检查与 `core/semgrep` 方法共用(方法是扩展进程，经环境变量 `TIGHTREIN_SEMGREP` 取得)。
- 确定性工具在 agent 运行前执行，此时 worktree 可写；工具产生的构建产物必须被被测项目的 `.gitignore` 忽略，否则会被 `guards` 在运行前的快照中记为已有改动。
- 没有 `static-tools` 的实现时只运行 Semgrep，运行摘要注明「未配置确定性工具」(10 篇 4.1)。
- 示例项目中的取值：技术栈扩展 `aspnetcore` 提供后端构建告警与依赖漏洞检查(10 篇 5.6)，示例项目的项目扩展以 `extend` 方式追加前端构建告警与 `npm audit`(10 篇 6.5)；`semgrep.configs` 为 `p/csharp`、`p/javascript`。

### 5.4 经 runner 调用的 agent 审查

`Reviewer` 协议定义在 `static/reviewer.py`，实现在 `pipeline/collect/prompts/`(见 05 篇)：

```
class Reviewer(Protocol):
    def review(self, scope: Scope, tool_findings: list[ToolFinding]) -> ReviewResult
    def review_baseline(self, batch: Batch, tool_findings: list[ToolFinding]) -> ReviewResult
    def scan_variants(self, pattern_id: str, scope: Scope) -> ReviewResult
    def verify(self, claim: Claim) -> Verification
```

| 调用 | 执行器任务 | 输出 schema |
|---|---|---|
| `review` | 在只读 worktree 中审查 diff 范围：对照预取的缺陷模式逐条执行其中的检出方法，筛查确定性工具的结果，同时使用 `differential-review` 做安全向差异审查；命中已接受取舍的主张不报告，列入 `excluded` 并写明取舍编号 | `runner/roles/static-review.schema.json` |
| `review_baseline` | 审查一批文件的现有代码本身，不看 diff：异常被吞或掩盖、资源未释放、重试与超时、边界与空值、并发与一致性、写入的原子性、日志与可观测性、接口的输入校验与权限、凭证与敏感信息；每条主张带严重度；使用 `sharp-edges` | 同上 |
| `scan_variants` | 以一个缺陷模式为种子，使用 `variant-analysis` 在全仓库查找同类实例 | 同上 |
| `verify` | 只给主张与位置，不给审查过程；使用 `skills/triage/references/roles/claim-verifier.md` | `runner/roles/claim-verifier.schema.json` |

| `Claim` 字段 | 说明 |
|---|---|
| `file`、`line` | 位置 |
| `ruleOrPattern` | 缺陷模式编号(如 `DP-0012`)、Semgrep 规则、告警代码或漏洞编号 |
| `layer` | `deterministic`、`incremental`、`full`、`baseline` |
| `severity` | `high`、`medium`、`low`；基线审查必填，其余可空 |
| `statement` | 疑似问题 |
| `trigger` | 触发条件 |

**基线审查的分批**(`baseline.py`)：扫描范围内的文件先去掉匹配 `sources.static.baseline.exclude` 的(写法同 `protectedPaths`)、开头 8 KB 含 NUL 字节的(二进制)与空文件；其余按路径排序后按所在目录分组，相邻的目录(小模块)在不超过 `batchFiles`、`batchLines` 时并入同一批，减少审查调用次数；一个目录超过上限时按文件顺序拆开，单个文件超过行数上限时独占一批。每批一次只读任务，模型按调用点 `collect.baseline-review` 的路由，上限取 `stages.collect.tasks.baseline-review`，当天预算照常受 `stages.collect.budgetPerDay` 约束。运行摘要列出批数、每批的文件数、行数、耗时与费用及合计；`stats` 带 `baselineBatches`、`baselineFiles`、`baselineExcludedFiles`、`baselineDurationMs`、`baselineCostUsd`(有费用数据时)。

`ReviewResult` 另带该次调用的耗时、费用与「当天预算已用尽」标记。`Verification` 取 `claim-verifier` 的输出：四档判定、`文件:类名.方法名`、带 `文件路径:行号` 的证据、调用链、任务外发现。任务的 `access` 为 `read-only`，`allowedCommands` 为只读 git 命令，轮数与预算取 `stages.collect`。

### 5.5 信号字段

只有判定为 `confirmed` 或 `conditional` 的主张产出信号；`insufficient` 计入 `stats.insufficientClaims`，`refuted` 只记事件日志。

| 字段 | 取值 |
|---|---|
| `source` | `synthetic` |
| `check` | `Claim.ruleOrPattern` |
| `location` | `Verification` 给出的 `文件:类名.方法名`；依赖漏洞为 `<依赖清单文件>:<包名>` |
| `message` | 主张的一句话描述 |
| `occurred_at` | 取证完成时间 |
| `release` | `head` |
| `actor` | 空 |

| `context` 字段 | 说明 |
|---|---|
| `line` | 行号 |
| `layer` | 来自哪一层扫描 |
| `verdict` | `confirmed` 或 `conditional`，以及条件 |
| `evidence` | 证据列表(`文件路径:行号` 与说明) |
| `callChain` | 调用链 |
| `introducedBy` | `git blame` 得到的 commit、作者、日期 |
| `toolFinding` | 来自确定性工具时的原始条目 |
| `transcripts` | 审查与取证的会话记录路径 |

**覆盖范围**：`coverage.files` 为本次扫描范围内的文件，`full`、`baseline` 档位为全部受检文件；2.8 中 static 的「已解决」判定依赖它。

### 5.6 错误处理

| 情况 | 处理 |
|---|---|
| 只读 worktree 不在目标 commit | 不运行，提示「先执行 `tightrein project worktree sync`」 |
| `static-tools` 中某个工具为 `failed`，或 Semgrep 失败 | 该工具的结果为空，其余照常，`status` 为 `partial`，输出保存到 `raw/static/<工具>.log` |
| `static-tools` 整体返回错误或超时 | 技术栈工具的结果为空，Semgrep 与审查照常，`status` 为 `partial`，扩展的 `message` 与 `hint` 写入 `notes` |
| `review` 或 `verify` 返回 `schema-invalid`、`limit-reached`、`failed` | 该次调用的主张不产出信号，`status` 为 `partial`，原因写入 `notes` |
| `guards.after` 发现只读 worktree 被改动、git 状态变化、不可写路径被修改(或 `guards.before` 发现凭证、无法读取 git 状态)，或探针比较的前后 git 状态不同 | 环境已不可信：整次运行 `failed`，不产出任何信号，运行摘要置顶提示 |
| 某次审查、扫描或取证调用的其他边界违规(读取了隐藏路径等) | 只作废该次调用的产出：审查或扫描的主张不取证，取证的结论不产出信号；其余任务照常，`status` 为 `partial`，`notes` 写「<任务> 的边界检查违规(<种类>)，该任务的产出作废」。违规种类经 `ReviewResult.violations`、`Verification.violations` 传给探针，判定为 `reviewer.environment_violated`(没有种类时按环境级处理) |
| 预算用尽 | 剩余疑点不取证，进入待处理清单(`stats.pendingOverLimit`)；基线审查中某批报告当天预算用尽时其余批次不再运行，`notes` 写明未审查的批数 |

### 5.7 测试

| 对象 | 方式 |
|---|---|
| `tools/extension.py` | 以 `static-tools` 的输出为夹具：条目转换、`incremental` 的文件过滤、依赖漏洞不受过滤、单个工具 `failed` 时为 `partial` |
| `tools/semgrep.py` | 以 Semgrep 的真实 JSON 输出为夹具，含 `errors` 与非 0 退出码 |
| `scope.py` | 在临时 git 仓库上构造提交，断言 diff 范围与跳过条件 |
| `baseline.py` | 在临时目录中构造文件，断言排除、二进制与空文件的识别、小模块合批、超限目录的拆分 |
| 探针流程 | 注入假的 `Reviewer`，断言只有成立的主张产出信号、低级与超额疑点进入待处理清单、遗留疑点先取证、选中的疑点只取证不审查、`guards` 违规时整次失败；第一次 `full` 按基线分批、当天预算用尽后不再运行其余批次 |
| 与 `runner` 联调 | 由 05 篇的回放测试覆盖(`--runner replay`) |

## 6. incidental

### 6.1 职责

把分诊与修复中顺带发现的缺陷(任务外发现)转成信号(1.6.5)。

### 6.2 数据来源

| 来源 | 读取方式 |
|---|---|
| 分诊与修复的交接文档 | `handoff_source.py` 查询 `handoffs` 表中 `stage` 为 `triage` 或 `fix`、状态为 `ok` 的记录，读取 `outputs.incidentalFindings` |
| 迁移归档 | `archive_import.py` 在 `--import-archive <目录>` 时扫描 markdown 报告中标题为「任务外发现」的一节，逐条列表项作为一条发现 |

按 10.2 的原则，交接文档是唯一的下游输入，日常运行不读人读文档；归档的 markdown 只在迁移时读取一次。

**已读记录**：`incidental_sources` 表，每个来源一行：

| 字段 | 说明 |
|---|---|
| `source_path` | 交接文档或归档文件的路径 |
| `content_hash` | 内容哈希 |
| `read_at` | 读取时间 |
| `signal_count` | 产出的信号数 |

路径与哈希都相同的来源跳过；交接文档重跑后内容变化，按新内容重新读取，重复的发现由 `aggregate` 按指纹归并。

### 6.3 定位与信号字段

交接文档中的 `incidentalFindings` 每项带结构化位置(`file`、`line`、`symbol`)与原文。归档条目只有原文，`locate.py` 从中提取 `文件路径:行号` 与 `类名.方法名`；提取不到文件的条目不产出信号，列入 `stats.unlocated` 与运行摘要，由用户手动补登。

| 字段 | 取值 |
|---|---|
| `source` | `synthetic` |
| `check` | `incidental` |
| `location` | `文件:类名.方法名`；没有方法名时为文件 |
| `message` | 发现原文 |
| `occurred_at` | 来源交接文档的 `createdAt`；归档条目取报告日期 |
| `release` | 分诊为 `triage_commit`，修复为修复分支的基准 commit；归档条目为空 |
| `actor` | 空 |

| `context` 字段 | 说明 |
|---|---|
| `line` | 行号 |
| `sourcePath` | 来源文件路径 |
| `sourceStage`、`sourceSubject`、`sourceRunId` | 来源的环节、对象与运行 |
| `sourceDate` | 来源日期 |

`coverage` 为空：任务外发现不作为任何问题的覆盖运行。

### 6.4 错误处理与测试

- 单个来源读取或解析失败时跳过该来源，不写已读记录，下次重试，在 `notes` 中列出。
- 测试：交接文档夹具(含与不含 `incidentalFindings`)、重跑后内容变化、归档 markdown 的多种写法、提取不到文件的条目。

## 7. 复现检查(pipeline/checks/regressions/)

### 7.1 清单格式

每个 Issue 一个目录 `regressions/<Issue 编号>/`，由核心在修复前写入(07 篇 4.3)，任何 agent 都不能写这个目录；探针只读取。清单的结构由 `contracts/schemas/data/regression.schema.json` 约束：

```
regressions/0007/
  check.yaml          清单
  api-1.request.json  API 类：请求
  api-1.expect.json   API 类：期望
  page-1.spec.ts      页面类：Playwright 用例
  static-1.yaml       静态类：Semgrep 规则
  test-1.py           测试类：测试文件的登记副本(<检查编号><后缀>)
```

| `check.yaml` 字段 | 说明 |
|---|---|
| `issue` | Issue 编号 |
| `problems` | 关联问题的指纹列表 |
| `checks[].id` | 检查编号，例如 `api-1` |
| `checks[].kind` | `api`、`page`、`static`、`test` |
| `checks[].role` | 执行时使用的角色(`api`、`page`) |
| `checks[].file` | 对应的文件；测试类为测试文件相对仓库根的路径(须匹配 `testPaths`，不得为绝对路径或含 `..`)，登记副本存为 `<检查编号><后缀>` |
| `checks[].location` | 检查对象的位置，写法与信号的 `location` 一致 |
| `checks[].targets` | 仅 `static`：规则作用的文件 |
| `checks[].command` | 仅 `test`：在修复 worktree 中运行这一个测试的命令，开头须为 `checks.commands` 中某条命令的允许前缀(07 篇 4.3)，执行目录为该命令的 `cwd` |
| `checks[].requires` | 接口、页面、静态类必填，测试类可省略：执行时需要的服务名，取自 `local-run` 扩展输出的 `services[].name` 与 `unavailable[].name`(10 篇 3.8)；示例项目中的取值为 `backend`、`frontend`、`compute` 的子集 |
| `checks[].precondition` | 可选：前置条件检查，例如「列表接口返回非空」；不满足时结果为弱证据 |

| 类型 | 通过条件 |
|---|---|
| `api` | 重放 `request.json` 得到的响应满足 `expect.json`：状态码条件(等于某值或不属于某类别)，可选的响应体 JSON Schema |
| `page` | 用例在 `regress-<角色>` 项目中通过(重试规则与巡检相同) |
| `static` | `semgrep scan --config <规则> --json --metrics=off <targets>` 没有结果 |
| `test` | `command` 在 worktree 中退出码为 0；退出码在 `regressions.testFailureExitCodes`(缺省 `[1]`)中为失败；其他非零(收集错误、用法错误、没有选中测试)为「无法执行」；无法启动或超时(`checks.timeoutSeconds`)为「未执行」；测试文件不在 worktree 中为「未执行」；失败输出命中 `regressions.testFailurePatterns.environment` 的重试 `regressions.envRetries` 次，仍失败为「未执行」 |

### 7.2 执行

```
run_checks(selection: RegressionSelection, target: ProbeTarget) -> list[RegressionCheckOutcome]
```

| 执行部分 | 复用 |
|---|---|
| `api_check.py` | `api_fuzz.replay`，登录复用 `common/session.py` |
| `page_check.py` | 页面运行器(第 3 节)：把选中的 `page-*.spec.ts` 所在目录作为 `regress-<角色>` 项目的 `testDir` |
| `static_check.py` | `static.tools.semgrep` |
| `repo_test_check.py` | 命令白名单由注入的 `pipeline/checks/project_checks.repro_test_cwd` 判断；日志写 `raw/<Issue>-<检查编号>.test.log` |

`RegressionCheckOutcome` 字段：`issue`、`checkId`、`kind`、`passed`、`preconditionMet`、`detail`(状态码、失败步骤或命中的规则位置)、`artifacts`。执行前核对清单条目与所引用文件的哈希与 `regressions.hash` 一致，不一致时直接报错。随探针执行的结果按 `RegressionResult` 枚举(`passed`、`failed`、`not-run`、`invalid`)写入 `regressions.last_result`。

| 使用者 | 选择 | 目标 | 结果去向 |
|---|---|---|---|
| `collect`(随 api-fuzz、static 运行) | 完成的 Issue 中与本方法对应类型的检查(页面类只在验证环节运行；测试类由修复第 7 步与部署后确认运行) | staging 或只读 worktree | 更新 `regressions` 表；失败的产出回归信号(7.3) |
| `fix apply`(第 7 步：本 Issue 与相关其他 Issue 的静态类与测试类) | 指定 Issue 的检查；回归检查为改动相关、最近通过的其他 Issue 的检查 | 修复 worktree | 写入修复交接文档(`otherRegressions`) |
| `verify local` | 接口与页面类：本 Issue 与相关其他 Issue 的检查(配置了 `local-run` 且改动涉及接口或页面时) | 本机启动的服务 | 写入验证报告 |
| `verify staging` | 指定 Issue 的检查(部署后确认，07 篇 14) | 接口与页面类为 staging；静态类与测试类为切到部署 commit 的只读 worktree | 写入验证报告 |

### 7.3 回归信号

| 字段 | 取值 |
|---|---|
| `source` | `synthetic` |
| `probe` | 检查类型对应的采集方法：`api` 为 `api-fuzz`，`static` 为 `static`；`page` 只在验证环节运行 |
| `check` | `regression` |
| `location` | `checks[].location` |
| `message` | `复现检查 <Issue 编号>/<检查编号> 失败：<detail 摘要>` |
| `release` | `target.release` |

`context` 带 `issue`、`checkId`、`targetFingerprints`(取自 `problems`)与 `detail`。`aggregate` 把回归信号直接归到 `targetFingerprints` 对应的问题(见 05 篇 3.4)。

### 7.4 错误处理与测试

- 清单不合格(缺字段、文件不存在)时该 Issue 的检查记为「无法执行」，不产出回归信号，在运行摘要中列出。
- 目标环境不可用或角色登录失败时检查记为「未执行」，与「失败」区分，不产出回归信号。
- 测试：清单校验、四类检查各自的通过与失败、「未执行」「无法执行」与「失败」的区分。

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
