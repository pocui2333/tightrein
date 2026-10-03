# 1. 信号采集

## 1.1 职责

从各个来源收集原始信号，转换成统一格式后交给「聚合去噪」。本模块不判断信号是否构成问题，也不做去重。

## 1.2 信号来源

| 类别 | 内容 | 典型采集方式 |
|---|---|---|
| 错误 | 后端未捕获异常、前端 JS 报错、接口 4xx/5xx | 错误监控 SDK、日志 |
| 性能 | 慢请求、超时、页面加载耗时 | APM、网关日志 |
| 行为 | 关键流程漏斗流失、连续快速点击(rage click)、点击无响应(dead click)、反复重试同一操作 | 前端埋点、会话回放 |
| 反馈 | 用户主动提交的反馈、客服记录 | 反馈入口、工单系统 |
| 合成 | 用脚本模拟用户在 staging 上跑核心流程，记录失败结果 | 定时运行的 E2E 脚本 |

## 1.3 统一信号格式

| 字段 | 说明 |
|---|---|
| `signalId` | 全局唯一 ID |
| `source` | 信号来源：error / performance / behavior / feedback / synthetic |
| `environment` | 所在环境：staging / production；collect 取 `target.environment`(缺省 staging) |
| `occurredAt` | 发生时间(UTC) |
| `release` | 发生时运行的版本号或 commit，用于把问题归因到具体提交 |
| `location` | 信号出现的位置：页面路由、接口路径或代码位置 |
| `message` | 原始描述：异常消息、事件名或反馈原文 |
| `context` | 附加上下文：堆栈、请求参数摘要、操作序列、耗时 |
| `actor` | 脱敏后的用户标识和角色，用于统计受影响人数 |

## 1.4 隐私与安全

- 在采集端完成脱敏：不采集密码、token 或完整请求体，个人信息做哈希处理。
- 下游 agent 只能读到脱敏后的信号，不能直接访问生产库和原始日志。

## 1.5 无用户阶段：主动制造信号

没有真实用户时，信号要靠自己「造」出来。有了用户以后，这些手段继续保留，作为线上的主动巡检。

**手段**：API 模糊测试(1.6.1)与静态巡检(1.6.4)主动制造信号；被测系统接入了错误追踪、日志或监控平台时，平台上的
运行报错、前端错误与业务告警也是信号来源(1.6.3)。采集中不跑页面脚本(1.6.2)。

**运行目标环境无关**

主动手段都通过一个「目标环境配置」来指定目标：访问地址、测试账号和数据准备方式。同一套脚本和 agent 不需要改动，就能在下面的环境里运行：

| 环境 | 目标 | 数据准备 | 触发时机 |
|---|---|---|---|
| staging | 已部署的测试环境 | 使用专用的测试账号和测试数据，定期清理 | 每次部署后执行，并且定时执行 |
| production(有用户以后) | 线上环境 | api-fuzz 只测 GET 且只测项目列出的允许接口，配置违反时启动报错 | 定时执行 |

**约束**

- 主动手段产出的信号，`source` 一律记为 `synthetic`，与平台上真实发生的错误分开统计。

## 1.6 落地选择

**通用与专属**：各探针的流程与判断规则是通用的，写在核心中；需要技术栈或项目知识的环节(导出接口描述、读取端点与角色的权限、读取与解析服务端日志、构建告警与依赖漏洞检查、前端页面路由、本机启动服务)都经扩展点完成，由技术栈扩展或项目扩展提供，扩展点的定义见 `docs/explanation/architecture/10-extensions.md`。本章以示例项目为例的内容一律标明「示例项目中的取值」或「示例项目的项目扩展」。

1. **API 模糊测试**：从只读的代码副本经 `spec-export` 扩展导出接口描述，再用 Schemathesis 对 staging 运行。这一项投入最小、见效最快。示例项目中的取值：后端配置了 Swagger(`Startup.cs`)，由技术栈扩展 `aspnetcore` 导出。
2. **页面测试**：采集中不做(1.6.2)，Playwright 只在验证环节运行。
3. **平台来源与项目探针**：被测系统内部的错误、访问日志与业务告警，只经它已接入的平台的只读查询 API 取得；没有业务监控时由项目探针补位(1.6.3)。
4. **静态巡检**：在本地的只读副本上定时运行，由 agent 对照项目的缺陷模式知识库审查新提交。
5. **任务外发现**：读取分诊与修复交接文档中的任务外发现(`outputs.incidentalFindings`)，转成信号。

**公共部分：运行机、触发与代码目录**

- **运行机**：用户本人的电脑，通过 macOS 的 launchd 定时运行。staging 在这台电脑上可以访问。
- **检测部署**：定时经扩展点 `deploy-source` 按平台只读读取最近的部署记录(`core/github-actions`、`core/github-deployments`、`core/vercel`，architecture/10 3.9)。发现新的 commit 已部署到 staging，就触发一次浅跑。示例项目选用 `core/github-actions`，工作流为 `deploy.yml`。没有配置部署来源的项目不做部署检测，探针运行的目标版本为空，部署后确认以合并时间加观察期为准(7.7)；没有被测地址(`target`)的项目，api-fuzz 记为 `skipped` 并写明原因；没有测试账号(`accounts`)的项目以匿名身份运行。
- **代码副本**：在工作区的 `worktrees/readonly/` 下维护一个项目仓库的只读 worktree，每次运行前切换到 staging 当前部署的 commit，用于导出接口描述和静态巡检，不在其中做任何修改。首次创建是 git 写操作，经用户确认后执行；此后的切换只移动它的游离 HEAD，不建分支、不提交。
- **项目知识库**：缺陷模式、已接受的取舍等项目知识放在工作区的 `knowledge/` 下(9.2)，不放在项目仓库或 worktree 中，agent 运行时由核心把所需条目作为上下文提供。
- **目录结构**：

目录结构见第 9 章：通用代码与 skill 在 `core/`、`skills/` 等目录中，示例项目的配置与数据在工作区 `workspaces/demo/` 中。本文其余章节提到的 `knowledge/`、`regressions/`、`data/`、`issues/` 等目录，都指 `workspaces/demo/` 下的对应目录，完整布局见 `docs/explanation/architecture/01-foundation.md` 4.3。

### 1.6.1 API 模糊测试

**原理**：Schemathesis 读取接口描述文件(OpenAPI)，按照每个参数的类型和约束自动生成大量合法与非法请求，发往目标环境，然后逐条检查响应。

**接口描述文件从哪来**：staging 上通常拿不到接口描述，因此在本地的只读代码副本上经 `spec-export` 扩展离线导出 OpenAPI 描述(`openapi.json`)，按 commit 缓存，运行时以文件形式传给 Schemathesis，再用 `--url` 指向 staging。怎样构建、用什么工具导出、启动期需要哪些假密钥，都由扩展负责。仓库中没有接口描述、由工作区维护一份时(`core/openapi-file` 的 `base: workspace`)直接读取工作区中的文件，不需要只读代码副本。

示例项目中的取值(技术栈扩展 `aspnetcore`，见 `docs/explanation/architecture/10-extensions.md` 5.3)：

- Swagger 只在开发环境开放，staging 上拿不到端点清单。
- 导出工具使用 `Swashbuckle.AspNetCore.Cli`，以全局 dotnet 工具的方式安装在本机。命令为 `dotnet swagger tofile --output <文件> <App.dll> V1`，其中 `V1` 与 `Startup.cs` 中 `SwaggerDoc` 的名称一致。
- CLI 的版本要与项目引用的 Swashbuckle 保持一致。
- `tofile` 会执行 `Startup` 中的服务注册，启动期需要的 JWT 密钥通过环境变量提供一份假值。

**鉴权**：每个角色准备一个专用测试账号，运行前按 `accounts.login` 调用登录接口换取 token，按角色分别运行。接口凭证是固定请求头((例如 `X-Access-Code`))的项目用 `accounts.login.kind: static-header`，钥匙串中的值原样放进该请求头，不请求登录接口；多数接口不需要登录的项目可以不配置 `accounts`，以匿名身份运行。账号密码与凭证保存在 macOS 钥匙串中，运行时读取。示例项目中的取值：接口默认要求 JWT 登录；三个角色(`Admin`、`Manager`、`User`)各有一个测试账号。

**检查项**

| 检查 | 能发现的问题 |
|---|---|
| 服务端 5xx(`not_a_server_error`) | 未处理的异常、参数校验缺失 |
| 响应与描述不一致(`response_schema_conformance`) | 返回结构、字段可空性与文档不符 |
| 未声明的状态码(`status_code_conformance`) | 错误处理不规范 |
| 忽略鉴权(`ignored_auth`) | 不带 token 或带无效 token 仍然返回数据。有状态测试下该检查有已知误报(官方 issue #2482)，结果需要复核 |
| 越权(自定义检查) | 某个角色不具备接口要求的能力，请求却成功了 |
| 响应过慢 | 超过设定阈值的接口 |

**越权检查**

- 数据来源有两份，都经扩展点取得：`authz-endpoints` 给出「端点 → 所需能力」，`authz-roles` 给出「角色 → 能力」。两份都有时才做越权检查；缺少任一份时不做越权检查，401、403 类的状态码不符也无法判断是否为预期的拒绝，只计数不产出信号。
- 检查规则：某个角色按角色能力不具备端点所要求的能力，请求却返回了 2xx，就判定为越权。
- 这项检查只覆盖端点级别的准入。方法体内按数据归属收窄的校验(例如「管理员只能操作本组织的数据」)，需要跨账号的数据才能验证，由后续的写流程巡检覆盖。
- 示例项目中的取值：「端点 → 所需能力」由技术栈扩展 `aspnetcore` 用反射从本地构建产物中读取 `[Authorize(Policy)]` 等声明，只读取构建产物；「角色 → 能力」由示例项目的项目扩展解析 `PermissionMatrix.cs` 得到。
- 自定义检查通过 Schemathesis 的 `@schemathesis.check` 编写，由 `schemathesis.toml` 的 `hooks` 配置加载。

**排除范围**：用 `--exclude-path-regex` 排除会锁定账号、产生资金往来或不适合模糊测试的接口，写在 `sources.api-fuzz.exclude`。示例项目中的取值：

- 登录接口：随机密码会触发登录失败锁定，把测试账号锁住。
- 支付接口：`PaymentController`。
- 文件上传与流式接口：`Controllers/Files/`。

**运行档位**

| 档位 | 触发 | 范围 |
|---|---|---|
| 浅跑 | 检测到 staging 完成新部署后 | `--max-examples 50 --include-method GET` |
| 深跑 | 每晚定时 | `--max-examples 200`，`--phases` 中加入 `stateful`，包含写接口，把多个接口串成业务链 |

鉴权：token 不放在命令行参数中，经 Schemathesis 子进程的环境变量 `TIGHTREIN_TOKEN` 传入，由本次运行生成的 `schemathesis.toml` 在 `headers` 中以 `Authorization = "Bearer ${TIGHTREIN_TOKEN}"` 引用。

**写接口**：staging 库的数据允许被测试污染，写接口在深跑中直接测试。写操作一律使用测试账号发起，产生的数据都归属于测试账号，需要时可以按归属统一清理。排除范围内的接口不因为允许写入而放开。

**输出**：一次运行产出三层结果，各有各的读者。

| 层 | 内容 | 读者 | 存放 |
|---|---|---|---|
| 原始报告 | `--report junit,vcr,ndjson`：JUnit 汇总通过与失败，VCR 是完整的请求响应录制，NDJSON 是逐条事件流，供转换脚本生成信号 | 需要追查细节时查看，以及转换脚本 | `data/runs/<运行编号>/raw/api-fuzz/`，按保留期清理 |
| 信号 | 每条失败转成一条统一格式的信号 | 下游模块(聚合去噪) | 数据库，原件为 `data/runs/<运行编号>/signals.ndjson` |
| 运行摘要 | 本次测了多少接口、失败多少、新增多少 | 用户本人，用于判断工具是否正常运转 | 本机通知，一次运行一条 |

**信号示例**(遵循 1.3 的统一格式，`context` 中是模糊测试特有的字段；取值为示例项目中的例子)：

```json
{
  "signalId": "S-01J9Z3K6Q4X8M2V7T5N0R1B3CD",
  "source": "synthetic",
  "environment": "staging",
  "occurredAt": "2026-09-29T02:15:03Z",
  "release": "d6f37025",
  "location": "POST /api/Order/Query",
  "message": "服务端返回 500",
  "context": {
    "probe": "api-fuzz",
    "runId": "R-20260929-021503-collect-api-fuzz",
    "check": "not_a_server_error",
    "role": "User",
    "request": { "query": {}, "body": { "pageSize": -1 } },
    "response": { "status": 500, "elapsedMs": 132, "bodyExcerpt": "..." },
    "reproduce": "curl -X POST 'https://<staging>/api/Order/Query' -H 'Authorization: Bearer <TOKEN>' -d '{\"pageSize\":-1}'",
    "reportPath": "data/runs/R-20260929-021503-collect-api-fuzz/raw/api-fuzz/User/"
  },
  "actor": { "id": "test-user", "role": "User" }
}
```

**字段约定**

- `probe`：信号来自哪一种采集方法(platform-errors / access-log / alerts / project-probe / api-fuzz / static / incidental)，下游据此区分同一 `source` 的不同来源。
- `check`：未通过的检查项名称，直接使用 Schemathesis 的检查名；越权等自定义检查使用自定义名称。
- `location` 使用接口的路由模板而不是实际 URL，使同一接口的失败能聚到一起。
- `release` 取检测部署时查到的 commit。
- `reproduce` 中的 token 一律替换为占位符，信号里不保存任何凭证。
- `bodyExcerpt` 截断到固定长度，并做 1.4 所述的脱敏处理。

是否构成问题、要不要提 Issue，由下游的分诊模块决定。

**参考**

- Schemathesis 官方论文：在 16 个服务中发现 755 个缺陷，检出数量是同类第二名工具的 1.4 到 4.5 倍。
- 常见用法是部署后浅跑、每晚深跑，并且始终以 staging 为目标。

### 1.6.2 页面测试

采集中不做页面测试：不在采集环节启动浏览器跑页面用例，项目也不需要为采集编写与维护页面用例(redesign/01-collect.md 第 2 节)。
浏览器引擎(Playwright)只用于验证环节：修复涉及前端时运行页面巡检与截图评审，以及页面类复现检查(architecture/04 第 3 节)。
被测系统还没有真实用户时，前端错误平台不会有数据，这一阶段前端的运行时问题只能由静态巡检与修复时的截图评审发现。

### 1.6.3 平台来源与项目探针

| 方法 | 取什么 | 怎么取 |
|---|---|---|
| 内部错误 | 应用运行报错(带堆栈)与前端错误(浏览器端 SDK) | 错误追踪平台(`core/sentry`)或集中日志平台(`core/loki` 加 `log-parse` 的字段映射)的只读查询 API，按时间窗口增量读取，保留期缺口写进运行摘要 |
| 访问日志(可选) | 按接口的耗时与 5xx 比例 | 经日志平台取访问日志，与基线比较，明显退化时产出信号；默认不启用 |
| 业务告警 | 已触发的业务告警 | 监控平台的只读 API(`core/alertmanager`)，基础设施类按标签或名称排除 |
| 项目探针 | 只有本项目知道的业务异常、项目规范的日志 | 工作区中的只读检查脚本(docs/how-to/write-project-probe.md) |

- 不登录服务器读取日志文件，不在服务器上执行命令，不在被测系统中增加任何组件；查询凭证只授予读权限，存放在本机钥匙串；取回的内容先脱敏再存储。
- 来自平台的信号直接用平台的分组编号作指纹(2.6)；每种方法默认不启用，配置了才启用，运行摘要列出未启用的方法。
- 被测系统没有接入这类平台时不启用，运行时缺陷由 api-fuzz 与静态巡检发现。
- 平台与方法的取舍、首批支持的平台与后续平台见 `docs/reference/methods.md`。

### 1.6.4 静态巡检

**原理**：不运行系统，直接读代码找问题。确定性工具先扫一遍，再由 LLM agent 对照项目自己总结的缺陷模式审查代码，最后逐条独立取证，只有证实成立的才产出信号。

**运行对象**：工作区中的只读 worktree(`worktrees/readonly/`)，切换到 `main` 的最新 commit。日常巡检针对上次巡检以来的新提交，也就是其他开发人员合入的改动；工作区的第一次巡检先对已有代码做一次基线审查。

**运行时间**：工作日固定时间运行，每天 1 到 2 次(例如上班前一次、午后一次)，由 launchd 定时触发。两次运行之间没有新提交时，确定性扫描和增量审查直接跳过。

**扫描层**

| 层 | 做法 | 运行时机 | 成本 |
|---|---|---|---|
| 确定性扫描 | 构建告警、依赖漏洞等与技术栈有关的检查经 `static-tools` 扩展运行；代码规则用 Semgrep 社区版，规则集取 `sources.static.semgrep.configs`。示例项目中的取值：技术栈扩展 `aspnetcore` 运行 `dotnet build` 告警与 `dotnet list package --vulnerable --include-transitive`，示例项目的项目扩展追加 `npm run build` 告警与 `npm audit --json`；Semgrep 规则集为 `p/csharp` 与 `p/javascript` | 每次运行 | 低 |
| 增量审查 | 取上次巡检以来的 diff，按缺陷模式条目(`knowledge/defect-pattern/`)的 `path:` 标签汇总出的目录路由，挑出与改动目录相关的缺陷模式，逐条执行其中的「怎么检出」；同时用 `differential-review` 做安全向的差异审查，用 `sharp-edges` 查容易误用的接口与危险默认值 | 每次运行 | 中 |
| 全量模式扫描 | 对 `knowledge/defect-pattern/` 中的每个缺陷模式，用 `variant-analysis` 在全仓库查找同类实例 | 每周第一个工作日的第一次运行 | 高 |
| 基线审查 | 不看 diff，把已有代码按目录模块分批(每批的文件数与总行数有上限，测试夹具、生成文件、锁文件、二进制与文档不审)，每批由强档模型整份审查：异常被吞或掩盖、资源未释放、重试与超时、边界与空值、并发与一致性、写入的原子性、日志与可观测性、接口的输入校验与权限、凭证与敏感信息；每条主张带严重度，取证有单独的上限，超出时按严重度保留 | 工作区第一次静态巡检的 `full` 档(没有上次巡检的终点)，或显式 `--level baseline`；之后的日常巡检仍是增量 | 高，一次性 |

**取证与过滤**

1. 各层扫描产出的都是「候选主张」，每条写明位置、疑似问题和触发条件。
2. 每条候选主张交给分诊中的 `claim-verifier` 角色(3.2)，由它在代码中独立取证，判定为成立、不成立或证据不足。交给它的只有主张和位置，不带扫描层的推理过程。
3. 命中工作区已接受取舍条目(`knowledge/tradeoff/`)的，直接丢弃。
4. 判定成立的产出信号；证据不足的计入运行摘要。

**运行方式**

- 由 agent 执行器(9.4)以无人值守、只读的方式在只读 worktree 中执行。工作目录就是项目仓库。使用本工具的 `collect` skill 中的静态巡检说明，以及与本工具 skill 一起安装的 `differential-review`、`sharp-edges`、`variant-analysis`(9.2)；所需的缺陷模式由核心从工作区 `knowledge/` 中取出作为上下文。
- 只开放读取、搜索和只读的 git 命令，约束方式见 9.4 与 9.5。
- 确定性工具产生的构建产物必须落在被项目 `.gitignore` 忽略的目录中。示例项目中的取值：`bin/`、`obj/` 和 `node_modules/`。

**输出**

- 信号：`source` 为 `synthetic`，`probe` 为 `static`。`location` 为 `文件:类名.方法名`，行号放在 `context` 中，`release` 为本次巡检的 commit。
- `context` 中附上命中的缺陷模式或规则名、`claim-verifier` 的判定与证据(代码位置和调用链)，以及用 `git blame` 取得的引入该问题的 commit。
- 原始报告：各工具的输出和 agent 的审查记录，存放在 `data/` 下。

**参考**

- 业界常见做法是先用确定性工具圈定候选，再让 LLM 做去重和误报过滤，Semgrep 的自动分诊和 Trail of Bits 的审查工具链都是这个思路。
- Semgrep 社区版对 C# 的解析率在 99.9% 以上，但很多依赖框架分析的 ASP.NET 规则只在 Pro 版生效，因此在示例项目中以项目自己的缺陷模式为主，Semgrep 为辅。
- `dotnet list package --vulnerable`(`aspnetcore` 技术栈扩展使用)从 GitHub Advisory Database 查询已知漏洞，发现漏洞时不会以非零退出码结束，需要解析输出来判断。

### 1.6.5 任务外发现

**来源**：分诊(第 3 章)与修复(第 5 章)在执行任务时，会把顺带撞见的、与本次任务无关的缺陷写进报告的「任务外发现」一节，按项目规则只列出不修改。这些发现是 agent 读代码时直接看到的，质量不低，需要有人跟进。例如时间以 UTC 写入却按本地时间显示、吞掉异常的 `catch` 等。

**采集**：

- 日常来源是分诊与修复的交接文档：每次分诊或修复结束后，读取交接文档中的 `outputs.incidentalFindings`，每项带结构化位置(`file`、`line`、`symbol`)与原文。按交接文档是唯一下游输入的原则(10.2)，日常运行不读人读报告。
- 迁移前的历史报告(9.10)只在执行 `collect --probe incidental --import-archive <目录>` 时读取一次，从报告的「任务外发现」一节逐条提取。
- 每条发现转成一条信号：`source` 为 `synthetic`，`probe` 为 `incidental`，`location` 为 `文件:类名.方法名`，`message` 为发现原文，`context` 中附上来源交接文档或报告的路径和日期。
- 已经读过的来源按文件路径和内容哈希记录，不重复读取；交接文档重跑后内容变化时按新内容重新读取，重复的发现由聚合按指纹归并。

**后续处理**：与其他信号一样进入聚合与分诊。这些发现没有经过取证，分诊时按 static 类问题处理，交给 `claim-verifier` 取证后才能成立。

**指纹**：与 static 相同，为 `incidental` + 文件 + 类名.方法名 + 规范化后的发现摘要。

## 1.7 有用户阶段
