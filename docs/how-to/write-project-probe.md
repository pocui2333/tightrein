# 如何编写项目探针

## 目标

为本项目写一个只读的检查脚本，发现只有本项目知道的业务异常(定时任务没有按时跑完、处理量突然为零、业务规则被违反)，
或按项目自己的日志规范检查日志(catch 后只记日志的异常、权限校验失败突增)。探针的输出进入归并、分诊与修复，之后的
流程与内置的采集方法相同。设计依据见 [采集](../explanation/redesign/01-collect.md) 第 3、4 节，输入输出的字段见
[项目探针契约](../reference/project-probe.md)。

## 前提

- 工作区已建好(`tightrein project init`)，`project.yaml` 可以通过 `tightrein project config`。
- 探针只从外部只读观察被测系统：调用只读接口或日志平台的查询 API，不修改数据，不登录服务器，不读服务器上的文件。
- 需要凭证时，先建一个只有读权限的凭证，存进本机钥匙串：`security add-generic-password -s <条目名> -a <账号> -w`。
- 要从日志平台取数时，工作区已配置 `extensions.log-platform`(例如 `core/loki`)；要按字段读取时再配置 `extensions.log-parse`。

## 步骤

1. **生成模板并登记**

   ```
   tightrein project probe new daily-import --every 1h --workspace workspaces/<项目>
   ```

   预期：输出「已生成 probes/daily_import.py 并登记到 sources.project-probes」；`project.yaml` 中多出：

   ```yaml
   sources:
     project-probes:
       - name: daily-import
         command: ["{python}", probes/daily_import.py]
         every: 1h
   ```

   探针需要钥匙串中的凭证时，在这一项加上 `keychain: [<条目名>]`(只有登记过的条目能被读取)；运行较久时加
   `timeoutSeconds`(缺省 300 秒)。`{python}` 是 tightrein 自己的解释器，脚本可以直接
   `from tightrein.sources.project_probes import helpers`。其他语言的脚本把 `command` 换成自己的命令即可。

2. **写检查逻辑**

   脚本从标准输入读一个 JSON(`name`、`lastRunAt`、`state`、`window`、`workspace`、`environment`、`baseUrl`)，向标准
   输出写一个 JSON(`signals`、`state`、`notes`)。每条信号写：

   | 字段 | 写什么 |
   |---|---|
   | `location` | 异常所在的位置：业务对象、任务名、接口或代码位置 |
   | `symptom` | 一句话的现象，写观察到的事实，不写原因 |
   | `evidence` | 可核对的事实：数值、时间、查询与结果；不写凭证与个人信息 |
   | `severityHint` | P0 到 P3，拿不准时为 null，分诊会重新判断 |
   | `fingerprint` | 同一个问题每次给出相同的值，例如「检查项:对象」；不要带时间或数量 |

   辅助函数(`helpers`)：`read_input()`、`signal(...)`、`emit(signals, state, notes)`(写之前按契约校验)、
   `secret(条目名)`(读钥匙串)、`redact(文本)`(脱敏)、`query_logs(查询, since, until)`(经日志平台取数)。
   两个完整示例见下文。

3. **单独试跑**

   ```
   tightrein project probe test daily-import --workspace workspaces/<项目>
   ```

   预期：输出「daily-import 的输出符合契约，将产出 N 条信号」，并逐条列出位置、现象、指纹与严重度提示。试跑不写数据库、
   不保存状态、不进入归并。输出不合格时列出原因与脚本的标准错误(已脱敏)，退出码为 1。

4. **进入流程**

   编排的 `sources` 步骤按 `every` 运行到期的探针并归并；也可以手动运行一次：

   ```
   tightrein collect --probe project-probe --select name:daily-import --workspace workspaces/<项目>
   tightrein aggregate --workspace workspaces/<项目>
   ```

   预期：collect 的运行摘要列出探针的信号数与说明；aggregate 之后新问题出现在 `tightrein status` 中。

## 示例一：业务异常(定时导入没有按时完成)

被测系统每天 02:00 前要完成一次数据导入，并提供只读接口 `GET /api/admin/jobs/import/latest` 返回最近一次导入的
完成时间与处理条数。探针检查「今天 02:00 之后仍没有完成」与「处理条数为零」。

```python
"""项目探针 daily-import：每天 02:00 前要完成一次数据导入，检查是否按时完成、处理量是否为零。"""

import json
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tightrein.sources.project_probes import helpers

LOCAL = ZoneInfo("Asia/Shanghai")
DEADLINE_HOUR = 2


def latest(base_url: str, token: str) -> dict:
    request = urllib.request.Request(f"{base_url}/api/admin/jobs/import/latest",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> None:
    found = helpers.read_input()
    token = helpers.secret("tightrein.demo.readonly")
    job = latest(found["baseUrl"], token)
    now = datetime.now(timezone.utc).astimezone(LOCAL)
    deadline = now.replace(hour=DEADLINE_HOUR, minute=0, second=0, microsecond=0)
    finished = datetime.fromisoformat(job["finishedAt"]).astimezone(LOCAL) if job.get("finishedAt") else None
    signals = []
    if now >= deadline and (finished is None or finished < deadline.replace(hour=0)):
        signals.append(helpers.signal(
            "job:daily-import", "今天的数据导入没有在 02:00 前完成",
            [f"最近一次完成时间 {finished.isoformat() if finished else '无'}", f"检查时间 {now.isoformat()}"],
            "missed:daily-import", severity_hint="P1"))
    elif job.get("processed") == 0:
        signals.append(helpers.signal(
            "job:daily-import", "最近一次数据导入处理了 0 条",
            [f"完成时间 {finished.isoformat()}", "processed=0"], "zero:daily-import", severity_hint="P2"))
    helpers.emit(signals, state={"lastFinishedAt": job.get("finishedAt")},
                 notes=[f"最近一次导入处理 {job.get('processed')} 条"])


if __name__ == "__main__":
    main()
```

登记：

```yaml
sources:
  project-probes:
    - name: daily-import
      command: ["{python}", probes/daily_import.py]
      every: 1h
      keychain: [tightrein.demo.readonly]
```

## 示例二：项目规范的日志(权限校验失败突增与只记日志的异常)

项目的日志规范要求权限校验失败记一条 `category=authz`、带 `role` 与 `endpoint` 字段的日志，catch 后不再抛出的异常记
`category=caught`、带 `exception` 与 `stack`。日志已送入 Loki，工作区配置了 `extensions.log-platform`(`core/loki`)
与 `extensions.log-parse`(`core/json-lines`，`categoryField: category`、`exceptionTypeField: exception`)。
探针按角色与接口统计权限校验失败，与上次的数量比较；每种被吞掉的异常一条信号。

```python
"""项目探针 log-rules：按项目日志规范检查权限校验失败的突增与 catch 后只记日志的异常。"""

import json
from collections import Counter

from tightrein.sources.project_probes import helpers

QUERY = '{app="api"} | json | category=~"authz|caught"'
SURGE_RATIO = 3
SURGE_MINIMUM = 20


def main() -> None:
    found = helpers.read_input()
    since, until = found["window"]["since"], found["window"]["until"]
    entries = helpers.query_logs(QUERY, since, until, limit=5000)
    denied: Counter[str] = Counter()
    caught: dict[str, dict] = {}
    for entry in entries:
        record = json.loads(entry["raw"])
        if entry["category"] == "authz":
            denied[f"{record.get('role')} {record.get('endpoint')}"] += 1
        elif entry["category"] == "caught" and entry["exception"]:
            caught.setdefault(entry["exception"]["type"], entry)
    previous = (found["state"] or {}).get("denied", {})
    signals = []
    for key, count in sorted(denied.items()):
        before = previous.get(key, 0)
        if count >= SURGE_MINIMUM and count >= SURGE_RATIO * max(before, 1):
            signals.append(helpers.signal(
                key.split(" ", 1)[1], f"{key} 的权限校验失败从 {before} 次增加到 {count} 次",
                [f"窗口 {since} 到 {until}", f"查询 {QUERY}"], f"authz-surge:{key}", severity_hint="P2"))
    for exception, entry in sorted(caught.items()):
        signals.append(helpers.signal(
            entry["message"] or exception, f"{exception} 被 catch 后只记了日志，没有向上抛出",
            [f"{entry['occurredAt']} {helpers.redact(entry['raw'])[:500]}"], f"caught:{exception}",
            severity_hint=None, occurred_at=entry["occurredAt"]))
    helpers.emit(signals, state={"denied": dict(denied)},
                 notes=[f"读取 {len(entries)} 条日志，权限校验失败 {sum(denied.values())} 次"])


if __name__ == "__main__":
    main()
```

登记：

```yaml
sources:
  project-probes:
    - name: log-rules
      command: ["{python}", probes/log_rules.py]
      every: 1h
```

## 验证

- `tightrein project probe test <名称>` 显示「输出符合契约」，信号的指纹在两次试跑之间相同。
- `tightrein collect --probe project-probe --select name:<名称>` 的交接文档中 `coverage.sources` 含探针名；
  `tightrein aggregate` 之后，同一指纹的异常归为同一个问题，探针某次运行不再输出它、累计覆盖运行达到次数后判为已解决。

## 常见问题

- **「输出不符合探针契约」**：按错误中的 JSON 路径补齐字段；`evidence` 至少一条，`severityHint` 不确定时写 null。
- **「钥匙串条目 … 没有在 sources.project-probes[].keychain 中登记」**：把条目名加进该探针的 `keychain`。
- **「没有配置 extensions.log-platform」**：`helpers.query_logs` 需要工作区配置日志平台方法，见[方法目录](../reference/methods.md)。
- **同一问题每次都成了新问题**：`fingerprint` 中带了时间、数量或随机值，应只由检查项与对象组成。
- **没有到期**：编排按 `every` 运行；`collect --select name:<名称>` 不看间隔，手动运行时总是执行。
