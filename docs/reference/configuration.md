# 配置字段

> 本文件由 `src/tightrein/cli/reference.py` 生成，不要手改；改了 `settings/defaults.json` 后运行 `python -m tightrein.cli.reference`。

取值的读取顺序：`settings/defaults.json` → `settings/controls.json`(本机) → 工作区 `settings.json` 的 `overrides`，后者覆盖前者。合并规则、`+` 追加、只许追加的列表与不能覆盖的项见 `settings/README.md`；`tightrein project config <键> --explain` 列出每一层给出的值。

下面列出 `defaults.json` 中的每个键与缺省值。

## models

模型别名：控制字段 `model`、`modelWhen`、`fallback` 写别名，这里定别名对应的工具、模型、推理强度与价格(每百万 token 的美元，工具不报费用时估算用)。

| 别名 | 工具 | 模型 | 推理强度 | 价格(输入/输出/缓存读/缓存写) |
|---|---|---|---|---|
| `opus` | claude | `opus` | high | 4 / 20 / 0.4 / 5 |
| `opus-mid` | claude | `opus` | medium | 4 / 20 / 0.4 / 5 |
| `fable` | claude | `fable` | — | — |
| `sonnet` | claude | `sonnet` | high | 3 / 15 / 0.3 / 3.75 |
| `flash` | agy | `gemini-3.8-flash-low` | — | 0.5 / 3 / 0.05 / 0.5 |
| `flash-mid` | agy | `gemini-3.8-flash-medium` | — | 0.5 / 3 / 0.05 / 0.5 |
| `flash-high` | agy | `gemini-3.8-flash-high` | — | 0.5 / 3 / 0.05 / 0.5 |

## controls

控制键写成「阶段.模块.小步骤」，字段按「小步骤 → 模块 → 阶段 → `*`」继承：下表每一行只列该控制键自己写的键，没写的取上一级。同一处也放各模块自己的参数(由模块按自己的 schema 校验)。

### 统一的控制字段(`*`)

| 字段 | 全局缺省 |
|---|---|
| `model` | `"opus"` |
| `modelWhen` | `{}` |
| `fallback` | `"sonnet"` |
| `timeout` | `"10m"` |
| `idle` | `"5m"` |
| `turns` | `15` |
| `inputTokens` | `50000` |
| `outputTokens` | `16000` |
| `rounds` | `1` |
| `access` | `"read"` |
| `network` | `false` |

### 各控制键

| 控制键 | 键 | 缺省值 |
|---|---|---|
| `collect` | `timeout` | `"30m"` |
| `collect` | `messageChars` | `1000` |
| `collect` | `evidenceBytes` | `16384` |
| `collect` | `excerptChars` | `2000` |
| `collect` | `sourceTimeout` | `"30m"` |
| `collect.static.review` | `model` | `"opus-mid"` |
| `collect.static.review` | `maxClaims` | `10` |
| `collect.static.verify` | `model` | `"opus-mid"` |
| `collect.static.baseline` | `model` | `"opus-mid"` |
| `collect.static.variant` | `model` | `"opus-mid"` |
| `collect.dedup` | `normalize` | `[]` |
| `collect.dedup` | `suppress` | `[]` |
| `collect.dedup` | `watchWindow` | `"24h"` |
| `collect.dedup` | `watchOccurrences` | `2` |
| `collect.dedup` | `coveredRuns."collect.static"` | `1` |
| `collect.dedup` | `coveredRuns."*"` | `3` |
| `collect.dedup` | `nearbyLines` | `10` |
| `collect.dedup` | `titleLength` | `120` |
| `collect.dedup` | `replayChecks."collect.api_fuzz"` | `["server_error"]` |
| `collect.dedup` | `replayAttempts` | `2` |
| `collect.dedup` | `falsePositiveCleanRuns` | `3` |
| `collect.project_probes` | `probes` | `[]` |
| `collect.project_probes` | `lookback` | `"24h"` |
| `collect.project_probes` | `probeTimeout` | `"5m"` |
| `collect.platform_errors` | `lookback` | `"24h"` |
| `collect.platform_errors` | `logQuery` | `null` |
| `collect.platform_errors` | `logLimit` | `5000` |
| `collect.platform_errors` | `levels` | `["error", "critical"]` |
| `collect.platform_errors` | `projectFrames` | `10` |
| `collect.platform_errors` | `logParse` | `"json_lines"` |
| `collect.platform_errors` | `sentry.projects` | `[]` |
| `collect.platform_errors` | `sentry.environment` | `null` |
| `collect.platform_errors` | `sentry.retentionDays` | `90` |
| `collect.platform_errors` | `sentry.limit` | `100` |
| `collect.platform_errors` | `sentry.breadcrumbs` | `10` |
| `collect.platform_errors` | `loki.retentionDays` | `30` |
| `collect.platform_errors` | `loki.pageSize` | `1000` |
| `collect.platform_errors` | `json_lines.timeField` | `"timestamp"` |
| `collect.platform_errors` | `json_lines.levelField` | `"level"` |
| `collect.platform_errors` | `json_lines.messageField` | `"message"` |
| `collect.platform_errors` | `json_lines.categoryField` | `null` |
| `collect.platform_errors` | `json_lines.eventIdField` | `null` |
| `collect.platform_errors` | `json_lines.exceptionTypeField` | `null` |
| `collect.platform_errors` | `json_lines.exceptionMessageField` | `null` |
| `collect.platform_errors` | `json_lines.timezone` | `"UTC"` |
| `collect.platform_errors` | `json_lines.levels.trace` | `"trace"` |
| `collect.platform_errors` | `json_lines.levels.trce` | `"trace"` |
| `collect.platform_errors` | `json_lines.levels.verbose` | `"trace"` |
| `collect.platform_errors` | `json_lines.levels.vrb` | `"trace"` |
| `collect.platform_errors` | `json_lines.levels.debug` | `"debug"` |
| `collect.platform_errors` | `json_lines.levels.dbug` | `"debug"` |
| `collect.platform_errors` | `json_lines.levels.dbg` | `"debug"` |
| `collect.platform_errors` | `json_lines.levels.information` | `"information"` |
| `collect.platform_errors` | `json_lines.levels.info` | `"information"` |
| `collect.platform_errors` | `json_lines.levels.inf` | `"information"` |
| `collect.platform_errors` | `json_lines.levels.notice` | `"information"` |
| `collect.platform_errors` | `json_lines.levels.warning` | `"warning"` |
| `collect.platform_errors` | `json_lines.levels.warn` | `"warning"` |
| `collect.platform_errors` | `json_lines.levels.wrn` | `"warning"` |
| `collect.platform_errors` | `json_lines.levels.error` | `"error"` |
| `collect.platform_errors` | `json_lines.levels.err` | `"error"` |
| `collect.platform_errors` | `json_lines.levels.fail` | `"error"` |
| `collect.platform_errors` | `json_lines.levels.critical` | `"critical"` |
| `collect.platform_errors` | `json_lines.levels.crit` | `"critical"` |
| `collect.platform_errors` | `json_lines.levels.crt` | `"critical"` |
| `collect.platform_errors` | `json_lines.levels.fatal` | `"critical"` |
| `collect.platform_errors` | `json_lines.levels.ftl` | `"critical"` |
| `collect.platform_errors` | `regex.pattern` | `"^(?P<time>\\S+ \\S+) (?P<level>[A-Za-z]+) (?P<message>.*)$"` |
| `collect.platform_errors` | `regex.timeFormat` | `null` |
| `collect.platform_errors` | `regex.timezone` | `"UTC"` |
| `collect.platform_errors` | `regex.levels.trace` | `"trace"` |
| `collect.platform_errors` | `regex.levels.trce` | `"trace"` |
| `collect.platform_errors` | `regex.levels.verbose` | `"trace"` |
| `collect.platform_errors` | `regex.levels.vrb` | `"trace"` |
| `collect.platform_errors` | `regex.levels.debug` | `"debug"` |
| `collect.platform_errors` | `regex.levels.dbug` | `"debug"` |
| `collect.platform_errors` | `regex.levels.dbg` | `"debug"` |
| `collect.platform_errors` | `regex.levels.information` | `"information"` |
| `collect.platform_errors` | `regex.levels.info` | `"information"` |
| `collect.platform_errors` | `regex.levels.inf` | `"information"` |
| `collect.platform_errors` | `regex.levels.notice` | `"information"` |
| `collect.platform_errors` | `regex.levels.warning` | `"warning"` |
| `collect.platform_errors` | `regex.levels.warn` | `"warning"` |
| `collect.platform_errors` | `regex.levels.wrn` | `"warning"` |
| `collect.platform_errors` | `regex.levels.error` | `"error"` |
| `collect.platform_errors` | `regex.levels.err` | `"error"` |
| `collect.platform_errors` | `regex.levels.fail` | `"error"` |
| `collect.platform_errors` | `regex.levels.critical` | `"critical"` |
| `collect.platform_errors` | `regex.levels.crit` | `"critical"` |
| `collect.platform_errors` | `regex.levels.crt` | `"critical"` |
| `collect.platform_errors` | `regex.levels.fatal` | `"critical"` |
| `collect.platform_errors` | `regex.levels.ftl` | `"critical"` |
| `collect.platform_errors` | `regex.multiline` | `true` |
| `collect.platform_errors` | `regex.rolloverToleranceMinutes` | `30` |
| `collect.access_log` | `lookback` | `"24h"` |
| `collect.access_log` | `query` | `null` |
| `collect.access_log` | `limit` | `20000` |
| `collect.access_log` | `fields.method` | `"method"` |
| `collect.access_log` | `fields.route` | `"route"` |
| `collect.access_log` | `fields.status` | `"status"` |
| `collect.access_log` | `fields.durationMs` | `"durationMs"` |
| `collect.access_log` | `pattern` | `null` |
| `collect.access_log` | `minRequests` | `20` |
| `collect.access_log` | `latencyRatio` | `2` |
| `collect.access_log` | `errorRateDelta` | `0.05` |
| `collect.access_log` | `baselineWeight` | `0.3` |
| `collect.access_log` | `scriptTimeout` | `"5m"` |
| `collect.access_log` | `loki.retentionDays` | `30` |
| `collect.access_log` | `loki.pageSize` | `1000` |
| `collect.alerts` | `exclude.names` | <details><summary>2 项</summary><code>["(?i)^(node\|kube\|container\|pod\|disk\|filesystem\|memory\|cpu\|load\|network\|host\|instance\|target)", "(?i)(restart\|oom\|watchdog\|infoinhibitor\|down$\|unreachable)"]</code></details> |
| `collect.alerts` | `exclude.labels.category` | `["infrastructure", "infra", "platform"]` |
| `collect.incidental` | `points."assess.triage"` | `"commit"` |
| `collect.incidental` | `points.implement` | `"baseCommit"` |
| `collect.api_fuzz` | `sourceTimeout` | `"45m"` |
| `collect.api_fuzz` | `maxExamples` | `100` |
| `collect.api_fuzz` | `phases` | `["examples", "coverage", "fuzzing"]` |
| `collect.api_fuzz` | `includeMethods` | `[]` |
| `collect.api_fuzz` | `exclude` | `[]` |
| `collect.api_fuzz` | `workers` | `1` |
| `collect.api_fuzz` | `fuzzTimeout` | `"30m"` |
| `collect.api_fuzz` | `sanitizeKeys` | `[]` |
| `collect.api_fuzz` | `reportErrorChars` | `200` |
| `collect.api_fuzz` | `healthTimeout` | `"10s"` |
| `collect.api_fuzz` | `openapi_url.timeoutSeconds` | `30` |
| `collect.static` | `sourceTimeout` | `"2h"` |
| `collect.static` | `semgrep.configs` | `[]` |
| `collect.static` | `extension` | `[]` |
| `collect.static` | `nonCode` | <details><summary>19 项</summary><code>["*.md", "*.rst", "*.txt", "docs/", "*.json", "*.yaml", "*.yml", "*.toml", "*.ini", "*.cfg", "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum", "*.snap", "__snapshots__/", "LICENSE*", ".gitignore"]</code></details> |
| `collect.static` | `maxVerify` | `20` |
| `collect.static` | `pendingLimit` | `200` |
| `collect.static` | `duplicateLines` | `10` |
| `collect.static` | `contextLines` | `20` |
| `collect.static` | `functionLines` | `150` |
| `collect.static` | `changesChars` | `60000` |
| `collect.static` | `knowledge.entries` | `8` |
| `collect.static` | `knowledge.tokens` | `3000` |
| `collect.static` | `baseline.maxClaims` | `40` |
| `collect.static` | `baseline.batchFiles` | `20` |
| `collect.static` | `baseline.batchLines` | `3000` |
| `collect.static` | `baseline.exclude` | <details><summary>14 项</summary><code>["fixtures/", "__fixtures__/", "testdata/", "__snapshots__/", "*.snap", "dist/", "build/", "out/", "coverage/", "node_modules/", "vendor/", "generated/", "*.min.js", "*.min.css"]</code></details> |
| `assess` | `timeout` | `"30m"` |
| `assess` | `perRun` | `10` |
| `assess` | `claimSamples` | `5` |
| `assess` | `insufficientToManual` | `3` |
| `assess` | `watchReopenOccurrences` | `3` |
| `assess` | `suppressionDays` | `30` |
| `assess` | `severityGuide` | `null` |
| `assess` | `dedup.candidateDays` | `90` |
| `assess` | `dedup.candidates` | `10` |
| `assess` | `dedup.sameTitle` | `0.9` |
| `assess` | `dedup.differentTitle` | `0.5` |
| `assess` | `refute.severities` | `["P0", "P1"]` |
| `assess` | `refute.taskTypes` | `["security", "data"]` |
| `assess` | `refute.impactKinds` | `["authorization", "data-ownership", "data-correctness", "credential-leak"]` |
| `assess` | `sizes.small.files` | `3` |
| `assess` | `sizes.medium.files` | `10` |
| `assess` | `treatment` | <details><summary>7 项</summary><code>[{"worth": ["wont"], "treatment": "wont_fix"}, {"severities": ["P0"], "treatment": "fix_now"}, {"severities": ["P1"], "verdicts": ["confirmed"], "treatment": "fix_now"}, {"worth": ["defer"], "treatment": "watch"}, {"severities": ["P1", "P2"], "treatment": "fix_later"}, {"severities": ["P3"], "sizes": ["small"], "worth": ["fix"], "treatment": "fix_later"}, {"treatment": "watch"}]</code></details> |
| `assess` | `vagueWords` | `["可能", "大概", "也许", "似乎", "建议进一步排查", "probably", "perhaps", "maybe", "おそらく", "たぶん", "かもしれ"]` |
| `assess.triage` | `model` | `"opus"` |
| `assess.triage` | `rounds` | `1` |
| `assess.refute` | `model` | `"flash-high"` |
| `assess.refute` | `fallback` | `"opus-mid"` |
| `assess.dedup` | `model` | `"flash"` |
| `assess.dedup` | `fallback` | `"opus-mid"` |
| `assess.issue` | `titleMaxLength` | `80` |
| `assess.issue` | `slugMaxLength` | `40` |
| `assess.issue` | `newAcceptanceMax` | `5` |
| `assess.issue` | `github.labelPrefix` | `"tightrein:"` |
| `implement` | `timeout` | `"2h"` |
| `implement` | `knowledgeEntries` | `5` |
| `implement` | `knowledgeTokens` | `3000` |
| `implement.prepare` | `links` | `[]` |
| `implement.locate` | `model` | `"flash-high"` |
| `implement.locate` | `fallback` | `"opus-mid"` |
| `implement.locate` | `timeout` | `"10m"` |
| `implement.design` | `model` | `"opus"` |
| `implement.design` | `modelWhen.high_risk` | `"fable"` |
| `implement.design` | `rounds` | `1` |
| `implement.design` | `timeout` | `"10m"` |
| `implement.design` | `riskRules.schema.paths` | `[]` |
| `implement.design` | `riskRules.schema.patterns` | `[]` |
| `implement.design` | `riskRules.authz.paths` | `[]` |
| `implement.design` | `riskRules.authz.patterns` | `[]` |
| `implement.design` | `riskRules.contract.paths` | `[]` |
| `implement.design` | `riskRules.contract.patterns` | `[]` |
| `implement.design.frontend` | `model` | `"flash-high"` |
| `implement.design.frontend` | `fallback` | `"opus-mid"` |
| `implement.design.frontend` | `timeout` | `"10m"` |
| `implement.design.frontend` | `paths` | <details><summary>18 项</summary><code>["*.vue", "*.jsx", "*.tsx", "*.html", "*.css", "*.scss", "*.sass", "*.less", "*.styl", "*.svelte", "public/", "static/", "assets/", "styles/", "components/", "views/", "pages/", "layouts/"]</code></details> |
| `implement.code` | `model` | `"opus"` |
| `implement.code` | `access` | `"write"` |
| `implement.code` | `turns` | `30` |
| `implement.code` | `timeout` | `"30m"` |
| `implement.code` | `checkpointRollbackFindings` | `3` |
| `implement.code` | `rounds` | `3` |
| `implement.check` | `environmentPatterns` | <details><summary>12 项</summary><code>["Connection refused", "ConnectionRefusedError", "ECONNREFUSED", "Read timed out", "TimeoutError", "ETIMEDOUT", "ECONNRESET", "ConnectionResetError", "socket\\.timeout", "OSError.*Errno", "address already in use", "EADDRINUSE"]</code></details> |
| `implement.check` | `environmentRetries` | `1` |
| `implement.check` | `testFailureExitCodes` | `[1]` |
| `implement.check` | `skipMarkers` | <details><summary>8 项</summary><code>[".skip(", "xit(", "[Fact(Skip", "[Ignore]", "@unittest.skip", "pytest.mark.skip", "@ts-ignore", "t.Skip("]</code></details> |
| `implement.check` | `residuePatterns` | `[]` |
| `implement.check` | `protectedMarkers` | `[]` |
| `implement.check` | `when` | `{}` |
| `implement.check` | `minLiteralLength` | `4` |
| `implement.check` | `outputChars` | `8000` |
| `implement.check` | `logTailBytes` | `1048576` |
| `implement.check.runtime` | `ports.api` | `null` |
| `implement.check.runtime` | `ports.backendForPages` | `null` |
| `implement.check.runtime` | `ports.frontend` | `null` |
| `implement.check.runtime` | `readyTimeout` | `"3m"` |
| `implement.check.runtime` | `stopTimeout` | `"15s"` |
| `implement.check.runtime` | `readyUrlTimeout` | `"5s"` |
| `implement.check.runtime` | `poll` | `"200ms"` |
| `implement.check.runtime` | `portProbeTimeout` | `"1s"` |
| `implement.check.runtime` | `scriptTimeout` | `"1m"` |
| `implement.check.runtime` | `api.spec` | `null` |
| `implement.check.runtime` | `api.endpoints` | `null` |
| `implement.check.runtime` | `api.maxExamples` | `20` |
| `implement.check.runtime` | `api.phases` | `["examples", "fuzzing"]` |
| `implement.check.runtime` | `api.workers` | `1` |
| `implement.check.runtime` | `api.timeout` | `"10m"` |
| `implement.check.runtime` | `api.errorChars` | `2000` |
| `implement.check.runtime` | `api.authHeader` | `"Authorization"` |
| `implement.check.runtime` | `api.authPrefix` | `"Bearer "` |
| `implement.check.runtime` | `pages.roles` | `["anonymous"]` |
| `implement.check.runtime` | `pages.locale` | `"zh-CN"` |
| `implement.check.runtime` | `pages.retries` | `2` |
| `implement.check.runtime` | `pages.timeout` | `"30m"` |
| `implement.check.runtime` | `pages.patrolGrep` | `null` |
| `implement.check.runtime` | `pages.ignoreRequests` | `[]` |
| `implement.check.runtime` | `pages.routes` | `null` |
| `implement.check.runtime` | `screenshots.pixelRatio` | `0.001` |
| `implement.check.runtime` | `screenshots.channelTolerance` | `8` |
| `implement.check.screenshots` | `model` | `"flash-mid"` |
| `implement.check.screenshots` | `fallback` | `"opus-mid"` |
| `implement.check.screenshots` | `timeout` | `"10m"` |
| `implement.review` | `model` | `"opus-mid"` |
| `implement.review` | `rounds` | `3` |
| `implement.review` | `timeout` | `"10m"` |
| `implement.review.deep` | `model` | `"flash-high"` |
| `implement.review.deep` | `fallback` | `"opus-mid"` |
| `implement.review.deep` | `timeout` | `"10m"` |
| `release` | `mergeMethod` | `"squash"` |
| `release` | `reviewComment` | `true` |
| `release.ci` | `watchInterval` | `"30s"` |
| `release.queue` | `timeLimit` | `"1h"` |
| `release.deploy` | `assumeDeployedAfter` | `"1h"` |
| `release.deploy` | `manualPaths` | `[]` |
| `release.deploy` | `github_actions.workflow` | `null` |
| `release.deploy` | `github_actions.branch` | `null` |
| `release.deploy` | `github_actions.limit` | `30` |
| `release.deploy` | `github_deployments.environment` | `null` |
| `release.deploy` | `github_deployments.limit` | `30` |
| `release.deploy` | `vercel.projectId` | `null` |
| `release.deploy` | `vercel.target` | `"production"` |
| `release.deploy` | `vercel.teamId` | `null` |
| `release.deploy` | `vercel.limit` | `30` |
| `release.accept` | `windows.default` | `"24h"` |
| `release.accept` | `windows."collect.platform_errors"` | `"24h"` |
| `release.accept` | `windows."collect.access_log"` | `"24h"` |
| `release.accept` | `windows."collect.alerts"` | `"24h"` |
| `release.accept` | `windows."collect.project_probes"` | `"24h"` |
| `release.accept` | `windows."collect.api_fuzz"` | `"24h"` |
| `release.accept` | `windows."collect.static"` | `"0s"` |
| `release.accept` | `windows."collect.incidental"` | `"0s"` |
| `retro.idea` | `model` | `"opus-mid"` |
| `retro.idea` | `turns` | `1` |
| `retro.idea` | `timeout` | `"2m"` |
| `retro` | `stepTokens` | `300000` |
| `retro` | `bigTokens` | `1000000` |
| `retro` | `issueTokens` | `1000000` |
| `retro` | `stepDuration` | `"20m"` |
| `retro` | `callDuration` | `"10m"` |
| `retro` | `rounds` | `2` |
| `retro` | `linesRead` | `5000` |
| `retro` | `repeatCount` | `2` |
| `retro` | `frequentCount` | `3` |
| `knowledge` | `matchEntries` | `8` |
| `knowledge` | `matchTokens` | `3000` |
| `knowledge` | `staleChangeRatio` | `0.5` |
| `knowledge.curate` | `model` | `"opus-mid"` |
| `knowledge.curate` | `turns` | `1` |
| `knowledge.curate` | `timeout` | `"3m"` |
| `knowledge.curate` | `candidates` | `5` |
| `knowledge.curate` | `candidateTokens` | `6000` |

## independence

`[["assess.refute", "assess.triage"], ["implement.review.deep", "implement.code"]]`

## limits

含义见 `src/tightrein/protocol/limits.md`。

| 键 | 缺省值 |
|---|---|
| `limits.retry.attempts` | `2` |
| `limits.retry.base` | `"1s"` |
| `limits.retry.max` | `"20s"` |
| `limits.retry.overloadMax` | `"60s"` |
| `limits.transientPatterns` | <details><summary>13 项</summary><code>["API error", "request failed", "stream was interrupted", "connection reset", ": EOF", "Service Unavailable", "UNAVAILABLE", "ECONNRESET", "ETIMEDOUT", "overloaded", "529", "rate limit", "stream disconnected"]</code></details> |
| `limits.breaker.dependencyFailures` | `5` |
| `limits.breaker.pause` | `"60s"` |
| `limits.breaker.objectFailures` | `3` |
| `limits.timeouts.httpConnect` | `"5s"` |
| `limits.timeouts.http` | `"30s"` |
| `limits.timeouts.git` | `"10m"` |
| `limits.timeouts.gitLowSpeedBytes` | `1000` |
| `limits.timeouts.gitLowSpeedTime` | `"60s"` |
| `limits.timeouts.ci` | `"30m"` |
| `limits.timeouts.tests` | `"15m"` |
| `limits.timeouts.semgrepRule` | `"5s"` |
| `limits.timeouts.semgrep` | `"10m"` |
| `limits.timeouts.command` | `"5m"` |
| `limits.timeouts.run` | `"4h"` |
| `limits.lock.heartbeat` | `"30s"` |
| `limits.lock.stale` | `"90s"` |
| `limits.shutdownGrace` | `"30s"` |

## resources

含义见 `src/tightrein/protocol/resources.md`。

| 键 | 缺省值 |
|---|---|
| `resources.concurrency.sessions` | `1` |
| `resources.concurrency.modelCalls` | `4` |
| `resources.concurrency.collectSources` | `4` |
| `resources.concurrency.tests` | `8` |
| `resources.concurrency.platformRps` | `5` |
| `resources.quota.reserveFiveHour` | `0.7` |
| `resources.quota.reserveWeekly` | `0.6` |
| `resources.quota.unknownResetWait` | `"1h"` |
| `resources.issueTokens` | `2000000` |
| `resources.cacheReadWeight` | `0.1` |

## boundaries

含义见 `src/tightrein/protocol/boundaries.md`。

| 键 | 缺省值 |
|---|---|
| `boundaries.readCommands` | <details><summary>12 项</summary><code>["git grep", "git ls-files", "git log", "git show", "git diff", "git blame", "grep", "ls", "cat", "head", "tail", "wc"]</code></details> |
| `boundaries.changeCap.files` | `10` |
| `boundaries.changeCap.lines` | `400` |
| `boundaries.autoApprove.files` | `3` |
| `boundaries.autoApprove.lines` | `100` |
| `boundaries.uncounted` | <details><summary>15 项</summary><code>["*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum", "__snapshots__/", "*.snap", "dist/", "build/", "*.min.js", "*.min.css", "generated/"]</code></details> |
| `boundaries.protected.forbidden` | `[".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "secrets*.json"]` |
| `boundaries.protected.highRisk` | <details><summary>18 项</summary><code>[".github/workflows/", ".gitlab-ci.yml", "Jenkinsfile", "package.json", "pyproject.toml", "requirements*.txt", "go.mod", "Cargo.toml", "Gemfile", "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "migrations/", "*.sql", "auth/", "*secret*", "*permission*"]</code></details> |
| `boundaries.gates.issue` | `"auto_low_risk"` |
| `boundaries.gates.design` | `"auto_within_threshold"` |
| `boundaries.gates.merge` | `"auto_ci_passed"` |

## records

含义见 `src/tightrein/protocol/records.md`。

| 键 | 缺省值 |
|---|---|
| `records.retention.runs` | `"90d"` |
| `records.retention.raw` | `"30d"` |
| `records.retention.operations` | `"30d"` |

## schedule

含义见 `src/tightrein/protocol/schedule.md`。

| 键 | 缺省值 |
|---|---|
| `schedule.tick` | `"15m"` |
| `schedule.window.from` | `"00:00"` |
| `schedule.window.to` | `"07:00"` |
| `schedule.window.days` | `["mon", "tue", "wed", "thu", "fri", "sat", "sun"]` |
| `schedule.every."collect.project_probes"` | `"1h"` |
| `schedule.every."collect.platform_errors"` | `"1h"` |
| `schedule.every."collect.access_log"` | `"1d"` |
| `schedule.every."collect.alerts"` | `"15m"` |
| `schedule.every."collect.api_fuzz"` | `"on_deploy"` |
| `schedule.every."collect.static"` | `"on_commit"` |
| `schedule.every."collect.incidental"` | `"1h"` |
| `schedule.advanceTo` | `"release"` |
| `schedule.logs.maxBytes` | `5242880` |
| `schedule.logs.keep` | `3` |

## git

含义见 `src/tightrein/protocol/git.md`。

| 键 | 缺省值 |
|---|---|
| `git.branch` | `"{prefix}{type}/{issue}-{slug}"` |
| `git.branchPrefix` | `""` |
| `git.commit` | `"{type}: {summary}"` |
| `git.types.bug` | `"fix"` |
| `git.types.feature` | `"feat"` |
| `git.pr` | `"{summary}"` |

## tools

| 键 | 缺省值 |
|---|---|
| `tools.claude.path` | `null` |
| `tools.agy.path` | `null` |
| `tools.codex.path` | `null` |
| `tools.semgrep.path` | `null` |
| `tools.gh.path` | `null` |
