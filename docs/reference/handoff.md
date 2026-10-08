# 交接格式

> 本文件由 `src/tightrein/cli/reference.py` 生成，不要手改；改了 schema 后运行 `python -m tightrein.cli.reference`。

每一步的交接都分四部分：结论、必填事实、量化数据、备注，格式与校验见 `src/tightrein/protocol/handoff.md`。下面是包内每个 schema(JSON Schema 2020-12)的字段：步骤交接的必填事实、agent 的结构化输出、项目脚本的输出。

| 文件 | 内容 |
|---|---|
| [`assess/assess.dedup.schema.json`](#assessassessdedupschemajson) | 评估的查重(assess.dedup) |
| [`assess/assess.triage.schema.json`](#assessassesstriageschemajson) | 评估的取证结论(assess.triage；证伪复核 assess.refute 用同一格式) |
| [`assess/handoff.schema.json`](#assesshandoffschemajson) | assess.triage 交接的必填事实 |
| [`collect/access_log/project_sources/output.schema.json`](#collectaccess_logproject_sourcesoutputschemajson) | 访问日志取数脚本的输出 |
| [`collect/incidental/finding.schema.json`](#collectincidentalfindingschemajson) | 任务外发现 |
| [`collect/project_probes/output.schema.json`](#collectproject_probesoutputschemajson) | 项目探针的输出 |
| [`collect/static/claims.schema.json`](#collectstaticclaimsschemajson) | 静态巡检的增量审查、基线审查与变体扫描输出的候选主张 |
| [`collect/static/tools/extension.schema.json`](#collectstatictoolsextensionschemajson) | 项目与技术栈确定性工具脚本的输出 |
| [`collect/static/verify.schema.json`](#collectstaticverifyschemajson) | 静态巡检一条主张的取证结论 |
| [`implement/check/runtime/launch.schema.json`](#implementcheckruntimelaunchschemajson) | 项目启动脚本输出的本机启动计划 |
| [`implement/check/runtime/screenshots.schema.json`](#implementcheckruntimescreenshotsschemajson) | 截图审查的结论(implement.check.screenshots) |
| [`implement/code/code.schema.json`](#implementcodecodeschemajson) | implement.code 的输出：实施结果 |
| [`implement/deliver/deliver.schema.json`](#implementdeliverdeliverschemajson) | implement.deliver 交给发布的必填事实(44c；键名照 release/record.parse_delivery) |
| [`implement/design/design.schema.json`](#implementdesigndesignschemajson) | implement.design 的输出：方案(只留编码、定案与审查用得上的字段) |
| [`implement/design/frontend.schema.json`](#implementdesignfrontendschemajson) | implement.design.frontend 的输出：前端设计说明 |
| [`implement/locate/locate.schema.json`](#implementlocatelocateschemajson) | implement.locate 的输出：补全的代码笔记 |
| [`implement/review/review.schema.json`](#implementreviewreviewschemajson) | 审查的结论(implement.review 与 implement.review.deep 共用) |
| [`knowledge/curate.schema.json`](#knowledgecurateschemajson) | knowledge.curate 的输出：一条建议沉淀与已有条目的关系，及要写入的完整条目 |
| [`retro/idea.schema.json`](#retroideaschemajson) | retro.idea 的输出：一条复盘记录的简单解决思路 |
| [`retro/retro.schema.json`](#retroretroschemajson) | retro.detect 交接的必填事实：新建与追加的记录、各自评级 |

## assess/assess.dedup.schema.json

评估的查重(assess.dedup)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sameRootCause` | 布尔 | 是 | — |
| `target` | 字符串(`^(P-)?\d{4,}$`) 或 null | 是 | — |
| `evidence` | 数组(元素：字符串(`^[^:\s][^:]*:\d+(-\d+)?$`)) | 是 | — |
| `reason` | 字符串 | 是 | — |

## assess/assess.triage.schema.json

评估的取证结论(assess.triage；证伪复核 assess.refute 用同一格式)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `analysis` | 字符串 | 是 | 先写：读了什么、调用链怎么走、为什么这样判 |
| `verdict` | `"confirmed"` \| `"conditional"` \| `"refuted"` \| `"insufficient"` | 是 | — |
| `facts` | 数组(元素：对象) | 是 | — |
| `facts[].location` | 字符串(`^[^:\s][^:]*:\d+(-\d+)?$`) | 是 | — |
| `facts[].observation` | 字符串 | 是 | — |
| `trigger` | 字符串 或 null | 是 | — |
| `counterEvidence` | 数组(元素：对象) | 是 | — |
| `counterEvidence[].check` | 字符串 | 是 | — |
| `counterEvidence[].entry` | 字符串 | 是 | — |
| `counterEvidence[].upstreamValidation` | 对象 | 是 | — |
| `counterEvidence[].upstreamValidation.status` | `"present"` \| `"absent"` | 是 | — |
| `counterEvidence[].upstreamValidation.location` | 字符串(`^[^:\s][^:]*:\d+(-\d+)?$`) 或 null | 是 | — |
| `counterEvidence[].result` | 字符串 | 是 | — |
| `impact` | 对象 或 null | 是 | — |
| `impact.kind` | `"authorization"` \| `"data-ownership"` \| `"data-correctness"` \| `"credential-leak"` \| `"core-flow-broken"` \| `"non-core-error"` \| `"contract-mismatch"` \| `"experience"` \| `"slow-response"` \| `"dependency-vulnerability"` | 是 | — |
| `impact.roles` | 数组(元素：字符串) | 是 | — |
| `impact.data` | 字符串 | 是 | — |
| `impact.callSites` | 数组(元素：字符串(`^[^:\s][^:]*:\d+(-\d+)?$`)) | 是 | — |
| `impact.consequence` | 字符串 | 是 | — |
| `sourceOfPhenomenon` | 对象 或 null | 是 | — |
| `sourceOfPhenomenon.location` | 字符串(`^[^:\s][^:]*:\d+(-\d+)?$`) 或 null | 是 | — |
| `sourceOfPhenomenon.factRef` | 整数 或 null | 是 | — |
| `sourceOfPhenomenon.explanation` | 字符串 | 是 | — |
| `rootCauses` | 数组(元素：对象) | 是 | — |
| `rootCauses[].file` | 字符串 | 是 | — |
| `rootCauses[].line` | 整数 | 是 | — |
| `rootCauses[].symbol` | 字符串 或 null | 是 | — |
| `fixedOnMain` | 对象 或 null | 是 | — |
| `fixedOnMain.commit` | 字符串(`^[0-9a-f]{7,40}$`) | 是 | — |
| `fixedOnMain.basis` | 字符串 | 是 | — |
| `tradeoffHit` | 字符串(`^(CON\|PAT\|LES)-\d{4,}$`) 或 null | 是 | — |
| `missingInfo` | 数组(元素：对象) | 是 | — |
| `missingInfo[].item` | 字符串 | 是 | — |
| `missingInfo[].source` | `"code"` \| `"user"` | 是 | — |
| `incidental` | 数组(元素：对象) | 是 | — |
| `incidental[].file` | 字符串 | 是 | 文件路径，相对仓库根 |
| `incidental[].line` | 整数 或 null | 是 | 行号；不确定时为 null(不参与指纹) |
| `incidental[].symbol` | 字符串 或 null | 是 | 函数或方法名(类名.方法名)；不在函数里时为 null |
| `incidental[].category` | 字符串 | 是 | 类别：defect(缺陷)、security(安全)、performance(性能)、data(数据)；命名、风格、重构建议等其他类别不收 |
| `incidental[].confidence` | `"confirmed"` \| `"suspected"` | 是 | 把握程度：confirmed(确定，看到了代码证据)、suspected(疑似) |
| `incidental[].evidence` | 字符串 | 是 | 一句证据：看到了什么代码或行为 |
| `incidental[].text` | 字符串 | 是 | 发现的原文：一句话写现象 |
| `report` | 对象 或 null | 是 | — |
| `report.title` | 字符串 | 是 | — |
| `report.summary` | 字符串 | 是 | — |
| `report.steps` | 数组(元素：字符串) | 是 | — |
| `report.expected` | 字符串 或 null | 是 | — |
| `report.actual` | 字符串 或 null | 是 | — |
| `report.acceptance` | 数组(元素：字符串) | 是 | — |
| `report.severity` | `"P0"` \| `"P1"` \| `"P2"` \| `"P3"` | 是 | — |
| `report.severityReason` | 字符串 | 是 | — |
| `assessment` | 对象 或 null | 是 | — |
| `assessment.worth` | `"fix"` \| `"optional"` \| `"defer"` \| `"wont"` | 是 | — |
| `assessment.worthReason` | 字符串 | 是 | — |
| `assessment.taskType` | `"bug"` \| `"security"` \| `"data"` \| `"frontend"` \| `"feature"` \| `"refactor"` \| `"dependency"` \| `"docs-config"` | 是 | — |
| `assessment.size` | `"small"` \| `"medium"` \| `"large"` | 是 | 粗规模档，只用于分流 |
| `assessment.files` | 数组(元素：对象) | 是 | — |
| `assessment.files[].path` | 字符串 | 是 | — |
| `assessment.files[].isNew` | 布尔 | 是 | — |
| `assessment.direction` | 字符串 | 是 | — |
| `assessment.flags` | 对象 | 是 | — |
| `assessment.flags.design` | 对象 | 是 | — |
| `assessment.flags.design.flagged` | 布尔 | 是 | — |
| `assessment.flags.design.reason` | 字符串 或 null | 是 | — |
| `assessment.flags.design.locations` | 数组(元素：字符串(`^[^:\s][^:]*:\d+(-\d+)?$`)) | 是 | — |
| `assessment.flags.dataStructure` | 对象 | 是 | — |
| `assessment.flags.dataStructure.flagged` | 布尔 | 是 | — |
| `assessment.flags.dataStructure.reason` | 字符串 或 null | 是 | — |
| `assessment.flags.dataStructure.locations` | 数组(元素：字符串(`^[^:\s][^:]*:\d+(-\d+)?$`)) | 是 | — |
| `assessment.flags.publicContract` | 对象 | 是 | — |
| `assessment.flags.publicContract.flagged` | 布尔 | 是 | — |
| `assessment.flags.publicContract.reason` | 字符串 或 null | 是 | — |
| `assessment.flags.publicContract.locations` | 数组(元素：字符串(`^[^:\s][^:]*:\d+(-\d+)?$`)) | 是 | — |
| `assessment.reevaluateWhen` | 字符串 或 null | 是 | — |
| `assessment.outOfScope` | 数组(元素：字符串) | 是 | — |
| `assessment.mustKeep` | 数组(元素：字符串) | 是 | — |
| `notes` | 数组(元素：对象) | 是 | 读到的、后面的步骤还会用到的位置(代码笔记) |
| `notes[].location` | 字符串(`^[^:\s][^:]*:\d+(-\d+)?$`) | 是 | — |
| `notes[].description` | 字符串 | 是 | — |
| `notes[].role` | `"core"` \| `"related"` | 是 | — |
| `knowledgeSuggestions` | 数组(元素：字符串) | 是 | 建议沉淀进知识库的规律，交用户确认 |

## assess/handoff.schema.json

assess.triage 交接的必填事实

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `problem` | 字符串(`^P-\d{4,}$`) | 是 | — |
| `verdict` | `"confirmed"` \| `"conditional"` \| `"refuted"` \| `"insufficient"` \| `"merged"` | 是 | — |
| `severity` | `"P0"` \| `"P1"` \| `"P2"` \| `"P3"` 或 null | 是 | — |
| `disposition` | `"fix_now"` \| `"fix_later"` \| `"watch"` \| `"wont_fix"` | 是 | — |
| `destination` | 字符串 或 null | 是 | — |
| `issue` | 字符串 或 null | 是 | — |
| `issues` | 数组(元素：字符串) | 是 | — |
| `mergedInto` | 字符串 或 null | 是 | — |
| `case` | `"verified"` \| `"reproducible"` \| `"full"` \| `"light"` | 否 | — |
| `knowledgeSuggestions` | 数组(元素：字符串) | 是 | — |
| `misjudged` | null 或 对象 | 是 | — |
| `misjudged.kind` | `"false_confirm"` \| `"false_refute"` \| `"false_block"` | 是 | — |
| `misjudged.point` | 字符串 | 是 | — |
| `misjudged.detail` | 字符串 | 是 | — |

## collect/access_log/project_sources/output.schema.json

访问日志取数脚本的输出

项目自己的取数脚本经标准输出交回的 JSON：访问日志原文的行(lines)，或已解析的请求(requests)，二选一；不合格时本次作废、读取位置不前进

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `lines` | 数组(元素：字符串) | 否 | 窗口内的访问日志原文，每项一行；解析按 controls."collect.access_log" 的 fields 或 pattern |
| `requests` | 数组(元素：对象) | 否 | 窗口内已解析的请求 |
| `requests[].method` | 字符串 | 是 | HTTP 方法 |
| `requests[].route` | 字符串 | 是 | 路由模板(如 /api/orders/{id})；带查询串时程序去掉 |
| `requests[].status` | 整数 | 是 | 响应状态码 |
| `requests[].durationMs` | 数字 或 null | 是 | 耗时(毫秒)；没有记录时为 null |
| `truncated` | 布尔 | 是 | 是否因条数上限只交回了窗口中的一部分 |
| `notes` | 数组(元素：字符串) | 否 | 写进运行摘要的说明 |

## collect/incidental/finding.schema.json

任务外发现

评估与实施中 agent 顺带发现、与当前任务无关的问题，写在交接文档 facts.incidentalFindings 的每一项。评估与实施的输出 schema 复制同样的结构；类别不是 defect、security、performance、data 的，采集时由程序丢弃

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | 字符串 | 是 | 文件路径，相对仓库根 |
| `line` | 整数 或 null | 是 | 行号；不确定时为 null(不参与指纹) |
| `symbol` | 字符串 或 null | 是 | 函数或方法名(类名.方法名)；不在函数里时为 null |
| `category` | 字符串 | 是 | 类别：defect(缺陷)、security(安全)、performance(性能)、data(数据)；命名、风格、重构建议等其他类别不收 |
| `confidence` | `"confirmed"` \| `"suspected"` | 是 | 把握程度：confirmed(确定，看到了代码证据)、suspected(疑似) |
| `evidence` | 字符串 | 是 | 一句证据：看到了什么代码或行为 |
| `text` | 字符串 | 是 | 发现的原文：一句话写现象 |

## collect/project_probes/output.schema.json

项目探针的输出

探针经标准输出返回的 JSON；不合格时本次作废、状态不保存，下次到期重试(契约见 collect/project_probes/README.md)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `signals` | 数组(元素：对象) | 是 | 发现的异常，每条一个信号；没有异常时为空数组 |
| `signals[].location` | 字符串 | 是 | 异常所在的位置：业务对象、任务名、接口或代码位置 |
| `signals[].symptom` | 字符串 | 是 | 现象，一句话，写观察到的事实不写原因 |
| `signals[].evidence` | 数组(元素：字符串) | 是 | 证据：可核对的事实(数值、时间、查询与结果)，不含凭据 |
| `signals[].severityHint` | `"P0"` \| `"P1"` \| `"P2"` \| `"P3"` \| `null` | 是 | 严重度提示(P0 到 P3)；不确定时为 null |
| `signals[].fingerprint` | 字符串 | 是 | 同一个问题每次给出相同的值(例如「检查项:对象」)，与探针名一起作为问题指纹 |
| `signals[].occurredAt` | 字符串(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) | 否 | 发生时间；省略时为本次运行的时间 |
| `signals[].context` | 对象 | 否 | 给评估看的补充数据(不含凭据与个人信息) |
| `state` | 对象 或 null | 否 | 留给下次运行的状态，下次原样放进输入的 state |
| `notes` | 数组(元素：字符串) | 否 | 写进运行摘要的说明 |

## collect/static/claims.schema.json

静态巡检的增量审查、基线审查与变体扫描输出的候选主张

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `claims` | 数组(元素：对象) | 是 | — |
| `claims[].file` | 字符串 | 是 | — |
| `claims[].line` | 整数 | 是 | — |
| `claims[].ruleOrPattern` | 字符串 | 是 | — |
| `claims[].layer` | `"deterministic"` \| `"incremental"` \| `"full"` \| `"baseline"` | 是 | — |
| `claims[].severity` | `"high"` \| `"medium"` \| `"low"` | 是 | — |
| `claims[].statement` | 字符串 | 是 | — |
| `claims[].trigger` | 字符串 | 是 | — |
| `excluded` | 数组(元素：对象) | 是 | — |
| `excluded[].file` | 字符串 | 是 | — |
| `excluded[].line` | 整数 | 是 | — |
| `excluded[].statement` | 字符串 | 是 | — |
| `excluded[].tradeoffId` | 字符串(`^(CON\|PAT\|LES)-\d{4,}$`) | 是 | — |

## collect/static/tools/extension.schema.json

项目与技术栈确定性工具脚本的输出

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `tools` | 数组(元素：对象) | 是 | — |
| `tools[].name` | 字符串 | 是 | — |
| `tools[].status` | `"ok"` \| `"failed"` \| `"skipped"` | 是 | — |
| `tools[].reason` | 字符串 或 null | 是 | — |
| `tools[].logFile` | 字符串 或 null | 否 | — |
| `findings` | 数组(元素：对象) | 是 | — |
| `findings[].tool` | 字符串 | 是 | — |
| `findings[].kind` | `"lint"` \| `"build-warning"` \| `"vulnerability"` | 是 | — |
| `findings[].rule` | 字符串 | 是 | — |
| `findings[].file` | 字符串 | 是 | — |
| `findings[].line` | 整数 或 null | 是 | — |
| `findings[].column` | 整数 或 null | 是 | — |
| `findings[].message` | 字符串 | 是 | — |
| `findings[].severity` | `"critical"` \| `"high"` \| `"medium"` \| `"low"` | 是 | — |
| `findings[].package` | 对象 或 null | 否 | — |
| `findings[].package.name` | 字符串 | 是 | — |
| `findings[].package.version` | 字符串 或 null | 否 | — |
| `findings[].package.advisoryUrl` | 字符串 或 null | 否 | — |

## collect/static/verify.schema.json

静态巡检一条主张的取证结论

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `analysis` | 字符串 | 是 | — |
| `verdict` | `"confirmed"` \| `"conditional"` \| `"refuted"` \| `"insufficient"` | 是 | — |
| `facts` | 数组(元素：对象) | 是 | — |
| `facts[].location` | 字符串(`^[^\s:][^:]*:\d+(-\d+)?$`) | 是 | — |
| `facts[].observation` | 字符串 | 是 | — |
| `trigger` | 字符串 或 null | 是 | — |
| `counterEvidence` | 数组(元素：对象) | 是 | — |
| `counterEvidence[].check` | 字符串 | 是 | — |
| `counterEvidence[].entry` | 字符串 | 是 | — |
| `counterEvidence[].upstreamValidation` | 对象 | 是 | — |
| `counterEvidence[].upstreamValidation.status` | `"present"` \| `"absent"` | 是 | — |
| `counterEvidence[].upstreamValidation.location` | 字符串(`^[^\s:][^:]*:\d+(-\d+)?$`) 或 null | 是 | — |
| `counterEvidence[].result` | 字符串 | 是 | — |
| `impact` | 对象 或 null | 是 | — |
| `impact.kind` | `"authorization"` \| `"data-ownership"` \| `"data-correctness"` \| `"credential-leak"` \| `"core-flow-broken"` \| `"non-core-error"` \| `"contract-mismatch"` \| `"experience"` \| `"slow-response"` \| `"dependency-vulnerability"` | 是 | — |
| `impact.callSites` | 数组(元素：字符串(`^[^\s:][^:]*:\d+(-\d+)?$`)) | 是 | — |
| `impact.consequence` | 字符串 | 是 | — |
| `sourceOfPhenomenon` | 对象 或 null | 是 | — |
| `sourceOfPhenomenon.location` | 字符串(`^[^\s:][^:]*:\d+(-\d+)?$`) 或 null | 是 | — |
| `sourceOfPhenomenon.explanation` | 字符串 | 是 | — |
| `rootCauses` | 数组(元素：对象) | 是 | — |
| `rootCauses[].file` | 字符串 | 是 | — |
| `rootCauses[].line` | 整数 | 是 | — |
| `rootCauses[].symbol` | 字符串 或 null | 是 | — |
| `missingInfo` | 数组(元素：对象) | 是 | — |
| `missingInfo[].what` | 字符串 | 是 | — |
| `missingInfo[].source` | `"code"` \| `"user"` | 是 | — |

## implement/check/runtime/launch.schema.json

项目启动脚本输出的本机启动计划

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `services` | 数组(元素：对象) | 是 | — |
| `services[].name` | 字符串(`^[A-Za-z0-9_.-]+$`) | 是 | — |
| `services[].role` | `"api"` \| `"pages"` \| `null` | 是 | — |
| `services[].cwd` | 字符串 | 是 | — |
| `services[].argv` | 数组(元素：字符串) | 是 | — |
| `services[].env` | 对象 | 是 | — |
| `services[].port` | 整数 | 是 | — |
| `services[].after` | 字符串 或 null | 是 | — |
| `services[].readyPatterns` | 数组(元素：字符串) | 是 | — |
| `services[].failPatterns` | 数组(元素：字符串) | 是 | — |
| `services[].readyUrl` | 字符串 或 null | 是 | — |
| `unavailable` | 数组(元素：对象) | 是 | — |
| `unavailable[].name` | 字符串 | 是 | — |
| `unavailable[].reason` | 字符串 | 是 | — |
| `migrationPaths` | 数组(元素：字符串) | 是 | — |

## implement/check/runtime/screenshots.schema.json

截图审查的结论(implement.check.screenshots)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `analysis` | 字符串 | 是 | — |
| `screenshots` | 数组(元素：对象) | 是 | — |
| `screenshots[].path` | 字符串 | 是 | — |
| `screenshots[].result` | `"ok"` \| `"issue"` \| `"unknown"` | 是 | — |
| `screenshots[].reason` | 字符串 | 是 | — |

## implement/code/code.schema.json

implement.code 的输出：实施结果

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `knowledgeSuggestions` | 数组(元素：字符串) | 是 | — |
| `analysis` | 字符串 | 是 | — |
| `status` | `"completed"` \| `"aborted"` | 是 | — |
| `changedFiles` | 数组(元素：字符串) | 是 | — |
| `testsWritten` | 数组(元素：字符串) | 是 | — |
| `verification` | 数组(元素：对象) | 是 | — |
| `verification[].command` | 字符串 | 是 | — |
| `verification[].output` | 字符串 | 是 | — |
| `deviations` | 数组(元素：字符串) | 是 | — |
| `bigIssue` | 对象 或 null | 是 | — |
| `bigIssue.description` | 字符串 | 是 | — |
| `bigIssue.locations` | 数组(元素：字符串) | 是 | — |
| `incidentalFindings` | 数组(元素：对象) | 是 | — |
| `incidentalFindings[].file` | 字符串 | 是 | — |
| `incidentalFindings[].line` | 整数 或 null | 是 | — |
| `incidentalFindings[].symbol` | 字符串 或 null | 是 | — |
| `incidentalFindings[].category` | `"defect"` \| `"security"` \| `"performance"` \| `"data"` | 是 | — |
| `incidentalFindings[].confidence` | `"confirmed"` \| `"suspected"` | 是 | — |
| `incidentalFindings[].evidence` | 字符串 | 是 | — |
| `incidentalFindings[].text` | 字符串 | 是 | — |
| `outOfScope` | 数组(元素：字符串) | 是 | — |
| `release` | 对象 或 null | 是 | — |
| `release.scope` | 字符串 或 null | 是 | — |
| `release.subject` | 字符串 | 是 | — |
| `release.why` | 字符串 | 是 | — |
| `release.prTitle` | 字符串 | 是 | — |
| `release.problem` | 字符串 | 是 | — |
| `release.approach` | 字符串 | 是 | — |
| `release.limitations` | 字符串 | 是 | — |

## implement/deliver/deliver.schema.json

implement.deliver 交给发布的必填事实(44c；键名照 release/record.parse_delivery)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `branch` | 字符串 | 是 | — |
| `worktree` | 字符串 | 是 | — |
| `commit` | 字符串(`^[0-9a-f]{40}$`) | 是 | — |
| `base` | 字符串 | 是 | — |
| `diffHash` | 字符串(`^[0-9a-f]{64}$`) | 是 | — |
| `changedFiles` | 数组(元素：对象) | 是 | — |
| `changedFiles[].path` | 字符串 | 是 | — |
| `changedFiles[].added` | 整数 | 是 | — |
| `changedFiles[].deleted` | 整数 | 是 | — |
| `changedFiles[].status` | 字符串 | 是 | — |
| `checks` | 数组(元素：对象) | 是 | — |
| `checks[].name` | 字符串 | 是 | — |
| `checks[].passed` | 布尔 | 是 | — |
| `checks[].detail` | 字符串 或 null | 是 | — |
| `acceptedFindings` | 数组(元素：字符串) | 是 | — |
| `review` | 对象 | 是 | — |
| `review.round` | 整数 或 null | 是 | — |
| `review.conclusions` | 数组(元素：字符串) | 是 | — |
| `highRisk` | 布尔 | 是 | — |
| `highRiskPaths` | 数组(元素：字符串) | 是 | — |
| `release` | 对象 | 是 | — |
| `release.title` | 字符串 或 null | 是 | — |
| `release.scope` | 字符串 或 null | 是 | — |
| `release.summary` | 字符串 或 null | 是 | — |
| `release.why` | 字符串 或 null | 是 | — |
| `release.problem` | 字符串 或 null | 是 | — |
| `release.approach` | 字符串 或 null | 是 | — |
| `release.limitations` | 字符串 或 null | 是 | — |
| `patch` | 字符串 | 是 | — |
| `unverified` | 数组(元素：字符串) | 是 | — |
| `knowledgeSuggestions` | 数组(元素：字符串) | 是 | — |
| `skipped` | null | 是 | — |
| `rounds` | 数组(元素：对象) | 是 | 全部轮次的汇总：每轮编码、自检、审查的结论与阻断项(按各轮落盘的交接) |
| `rounds[].round` | 整数 | 是 | — |
| `rounds[].steps` | 对象 | 是 | — |

## implement/design/design.schema.json

implement.design 的输出：方案(只留编码、定案与审查用得上的字段)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `knowledgeSuggestions` | 数组(元素：字符串) | 是 | — |
| `analysis` | 字符串 | 是 | — |
| `summary` | 字符串 | 是 | — |
| `hypothesis` | 对象 | 是 | — |
| `hypothesis.cause` | 字符串 | 是 | — |
| `hypothesis.evidence` | 数组(元素：对象) | 是 | — |
| `hypothesis.evidence[].location` | 字符串 | 是 | — |
| `hypothesis.evidence[].fact` | 字符串 | 是 | — |
| `hypothesis.edits` | 数组(元素：对象) | 是 | — |
| `hypothesis.edits[].location` | 字符串 | 是 | — |
| `hypothesis.edits[].change` | 字符串 | 是 | — |
| `steps` | 数组(元素：对象) | 是 | — |
| `steps[].file` | 字符串 | 是 | — |
| `steps[].change` | 字符串 | 是 | — |
| `steps[].verification` | 字符串 | 是 | — |
| `files` | 数组(元素：对象) | 是 | — |
| `files[].path` | 字符串 | 是 | — |
| `files[].isNew` | 布尔 | 是 | — |
| `files[].reason` | 字符串 或 null | 是 | — |
| `estimate` | 对象 | 是 | — |
| `estimate.files` | 整数 | 是 | — |
| `estimate.lines` | 整数 | 是 | — |
| `oversize` | 对象 或 null | 是 | — |
| `oversize.reason` | 字符串 | 是 | — |
| `oversize.parts` | 数组(元素：对象) | 是 | — |
| `oversize.parts[].title` | 字符串 | 是 | — |
| `oversize.parts[].goal` | 字符串 | 是 | — |
| `oversize.parts[].acceptance` | 数组(元素：字符串) | 是 | — |
| `protectedTouches` | 数组(元素：对象) | 是 | — |
| `protectedTouches[].path` | 字符串 | 是 | — |
| `protectedTouches[].change` | 字符串 | 是 | — |
| `protectedTouches[].reason` | 字符串 | 是 | — |
| `flags` | 对象 | 是 | — |
| `flags.design` | 对象 | 是 | — |
| `flags.design.flagged` | 布尔 | 是 | — |
| `flags.design.reason` | 字符串 或 null | 是 | — |
| `flags.dataStructure` | 对象 | 是 | — |
| `flags.dataStructure.flagged` | 布尔 | 是 | — |
| `flags.dataStructure.reason` | 字符串 或 null | 是 | — |
| `flags.publicContract` | 对象 | 是 | — |
| `flags.publicContract.flagged` | 布尔 | 是 | — |
| `flags.publicContract.reason` | 字符串 或 null | 是 | — |
| `migration` | 对象 或 null | 是 | — |
| `migration.entries` | 数组(元素：字符串) | 是 | — |
| `migration.reversible` | 布尔 | 是 | — |
| `migration.revertMethod` | 字符串 | 是 | — |
| `newDependencies` | 数组(元素：对象) | 是 | — |
| `newDependencies[].name` | 字符串 | 是 | — |
| `newDependencies[].version` | 字符串 | 是 | — |
| `newDependencies[].reason` | 字符串 | 是 | — |
| `deletions` | 数组(元素：对象) | 是 | — |
| `deletions[].path` | 字符串 | 是 | — |
| `deletions[].reason` | 字符串 | 是 | — |
| `acceptanceMapping` | 数组(元素：对象) | 是 | — |
| `acceptanceMapping[].criterion` | 字符串 | 是 | — |
| `acceptanceMapping[].steps` | 数组(元素：整数) | 是 | — |
| `userVisibleChange` | 字符串 | 是 | — |
| `affectedEndpoints` | 数组(元素：字符串) | 是 | — |
| `affectedPages` | 数组(元素：字符串) | 是 | — |
| `notDoing` | 数组(元素：字符串) | 是 | — |
| `userDecisions` | 数组(元素：对象) | 是 | — |
| `userDecisions[].question` | 字符串 | 是 | — |
| `userDecisions[].recommendation` | 字符串 | 是 | — |
| `userDecisions[].reason` | 字符串 | 是 | — |

## implement/design/frontend.schema.json

implement.design.frontend 的输出：前端设计说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pages` | 数组(元素：对象) | 是 | — |
| `pages[].location` | 字符串 | 是 | — |
| `pages[].structure` | 字符串 | 是 | — |
| `layout` | 数组(元素：字符串) | 是 | — |
| `interactions` | 数组(元素：对象) | 是 | — |
| `interactions[].target` | 字符串 | 是 | — |
| `interactions[].behavior` | 字符串 | 是 | — |
| `states` | 数组(元素：对象) | 是 | — |
| `states[].target` | 字符串 | 是 | — |
| `states[].loading` | 字符串 | 是 | — |
| `states[].empty` | 字符串 | 是 | — |
| `states[].error` | 字符串 | 是 | — |
| `styling` | 数组(元素：对象) | 是 | — |
| `styling[].target` | 字符串 | 是 | — |
| `styling[].value` | 字符串 | 是 | — |
| `styling[].source` | 字符串 | 是 | — |
| `mobile` | 数组(元素：字符串) | 是 | — |
| `copy` | 数组(元素：对象) | 是 | — |
| `copy[].location` | 字符串 | 是 | — |
| `copy[].text` | 字符串 | 是 | — |
| `copy[].key` | 字符串 或 null | 是 | — |
| `planConflicts` | 数组(元素：字符串) | 是 | — |

## implement/locate/locate.schema.json

implement.locate 的输出：补全的代码笔记

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `knowledgeSuggestions` | 数组(元素：字符串) | 是 | — |
| `analysis` | 字符串 | 是 | — |
| `core` | 数组(元素：对象) | 是 | — |
| `core[].location` | 字符串 | 是 | — |
| `core[].description` | 字符串 | 是 | — |
| `related` | 数组(元素：对象) | 是 | — |
| `related[].location` | 字符串 | 是 | — |
| `related[].description` | 字符串 | 是 | — |
| `trigger` | 字符串 或 null | 是 | — |
| `affectedEndpoints` | 数组(元素：字符串) | 是 | — |
| `affectedPages` | 数组(元素：字符串) | 是 | — |
| `missing` | 数组(元素：字符串) | 是 | — |
| `incidentalFindings` | 数组(元素：对象) | 是 | — |
| `incidentalFindings[].file` | 字符串 | 是 | — |
| `incidentalFindings[].line` | 整数 或 null | 是 | — |
| `incidentalFindings[].symbol` | 字符串 或 null | 是 | — |
| `incidentalFindings[].category` | `"defect"` \| `"security"` \| `"performance"` \| `"data"` | 是 | — |
| `incidentalFindings[].confidence` | `"confirmed"` \| `"suspected"` | 是 | — |
| `incidentalFindings[].evidence` | 字符串 | 是 | — |
| `incidentalFindings[].text` | 字符串 | 是 | — |

## implement/review/review.schema.json

审查的结论(implement.review 与 implement.review.deep 共用)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `analysis` | 字符串 | 是 | — |
| `acceptance` | 数组(元素：对象) | 是 | — |
| `acceptance[].criterion` | 字符串 | 是 | — |
| `acceptance[].result` | `"pass"` \| `"fail"` \| `"unknown"` | 是 | — |
| `acceptance[].reason` | 字符串 | 是 | — |
| `previousBlockers` | 数组(元素：对象) | 是 | — |
| `previousBlockers[].location` | 字符串 或 null | 是 | — |
| `previousBlockers[].kind` | 字符串 | 是 | — |
| `previousBlockers[].verdict` | `"resolved"` \| `"unresolved"` \| `"misjudged"` | 是 | — |
| `previousBlockers[].reason` | 字符串 | 是 | — |
| `blockers` | 数组(元素：对象) | 是 | — |
| `blockers[].kind` | 字符串 | 是 | — |
| `blockers[].location` | 字符串 或 null | 是 | — |
| `blockers[].trigger` | 字符串 或 null | 是 | — |
| `blockers[].problem` | 字符串 | 是 | — |
| `blockers[].category` | `"local"` \| `"plan_gap"` \| `"needs_user"` \| `"design"` | 是 | — |
| `blockers[].rootCause` | 字符串 | 是 | — |
| `unverified` | 数组(元素：对象) | 是 | — |
| `unverified[].item` | 字符串 | 是 | — |
| `unverified[].reason` | 字符串 | 是 | — |
| `incidentalFindings` | 数组(元素：对象) | 是 | — |
| `incidentalFindings[].file` | 字符串 | 是 | — |
| `incidentalFindings[].line` | 整数 或 null | 是 | — |
| `incidentalFindings[].symbol` | 字符串 或 null | 是 | — |
| `incidentalFindings[].category` | `"defect"` \| `"security"` \| `"performance"` \| `"data"` | 是 | — |
| `incidentalFindings[].confidence` | `"confirmed"` \| `"suspected"` | 是 | — |
| `incidentalFindings[].evidence` | 字符串 | 是 | — |
| `incidentalFindings[].text` | 字符串 | 是 | — |
| `knowledgeSuggestions` | 数组(元素：字符串) | 是 | — |

## knowledge/curate.schema.json

knowledge.curate 的输出：一条建议沉淀与已有条目的关系，及要写入的完整条目

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `analysis` | 字符串 | 是 | 先写：建议讲的是什么规律，与每条候选是不是同一件事 |
| `decision` | `"add"` \| `"update"` \| `"merge"` \| `"noop"` | 是 | — |
| `targetIds` | 数组(元素：字符串) | 是 | update 恰好 1 条；merge 与 noop 至少 1 条；add 为空 |
| `supersedes` | 数组(元素：字符串) | 是 | 只有 add 可填：被推翻的候选 |
| `entry` | null 或 对象 | 是 | add、update、merge 时为完整条目；noop 时为 null |
| `entry.kind` | `"conventions"` \| `"patterns"` \| `"lessons"` | 是 | — |
| `entry.slug` | 字符串(`^[a-z0-9]+(-[a-z0-9]+)*$`) | 是 | — |
| `entry.title` | 字符串 | 是 | — |
| `entry.summary` | 字符串 | 是 | — |
| `entry.locations` | 数组(元素：字符串(`^(path\|route\|page):\S`)) | 是 | — |
| `entry.body` | 字符串 | 是 | — |
| `reason` | 字符串 | 是 | — |

## retro/idea.schema.json

retro.idea 的输出：一条复盘记录的简单解决思路

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `idea` | 字符串 | 是 | 一两句话的解决思路 |
| `settle` | 字符串 或 null | 是 | 值得沉淀为经验或规则时写一句要沉淀的内容；不值得时为 null |

## retro/retro.schema.json

retro.detect 交接的必填事实：新建与追加的记录、各自评级

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `created` | 数组(元素：对象) | 是 | — |
| `created[].id` | 字符串(`^\d{4,}$`) | 是 | — |
| `created[].rating` | `"P0"` \| `"P1"` \| `"P2"` \| `"P3"` | 是 | — |
| `created[].kind` | `"failure"` \| `"waste"` \| `"misjudgment"` \| `"interruption"` | 是 | — |
| `created[].point` | 字符串 | 是 | — |
| `created[].count` | 整数 | 是 | — |
| `appended` | 数组(元素：对象) | 是 | — |
| `appended[].id` | 字符串(`^\d{4,}$`) | 是 | — |
| `appended[].rating` | `"P0"` \| `"P1"` \| `"P2"` \| `"P3"` | 是 | — |
| `appended[].kind` | `"failure"` \| `"waste"` \| `"misjudgment"` \| `"interruption"` | 是 | — |
| `appended[].point` | 字符串 | 是 | — |
| `appended[].count` | 整数 | 是 | — |
| `errors` | 数组(元素：字符串) | 是 | 检测与写解决思路时出的错；不影响其余各项 |
