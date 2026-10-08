# 方法 3：按项目日志规范统计

## 介绍

项目有自己的日志规范：权限校验失败记一条 `category=authz`、带 `role` 与 `endpoint`；catch 后不再抛出的异常记
`category=caught`、带 `exception`。这些不是错误级别、平台错误模块不会收。本方法经日志平台按规范字段统计，发现权限
校验失败的突增与「只记日志就吞掉的异常」。适用于日志已送入日志平台(Loki)且有规范字段的项目。

## 实现方式

- 按 LogQL 查询窗口内的规范日志：`{app="api"} | json | category=~"authz|caught"`；
- 权限校验失败按「角色 接口」计数，与上次的数量比较：本次不少于 20 次且不少于上次的 3 倍为突增；
- 每种被吞掉的异常类型一条信号；
- 阈值写在脚本顶部，按项目调整。

## 运行模式

- 按 `every` 定时运行(如 `1h`)，窗口为上次成功运行到现在；
- 只读：只调日志平台的查询 API；
- 跨次状态：上次各「角色 接口」的失败次数(`denied`)，用于判断突增。

## 架构设计

tightrein 启动脚本给出窗口；脚本按 sites.json 中 `loki` 的地址、用 secrets 中登记的 `loki.token` 调 Loki 的
`/loki/api/v1/query_range`(与平台错误模块用的是同一个只读 API)，统计后经 `helpers.emit` 输出。

## 代码排版

`workspaces/<项目>/scripts/log_rules.py`：常量(查询、阈值) → `query()` 读日志平台 → `main()` 统计、判定、输出。

```python
"""项目探针 log-rules：按项目日志规范检查权限校验失败的突增与 catch 后只记日志的异常。"""

import json
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

from tightrein.collect.project_probes import helpers

LOKI = "https://logs.example.com"          # 与 sites.json 的 loki.url 相同
QUERY = '{app="api"} | json | category=~"authz|caught"'
SURGE_RATIO = 3
SURGE_MINIMUM = 20
LIMIT = 5000


def nanoseconds(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()) * 10 ** 9


def query(since: str, until: str) -> list[dict]:
    params = urllib.parse.urlencode({"query": QUERY, "start": nanoseconds(since), "end": nanoseconds(until),
                                     "limit": LIMIT, "direction": "forward"})
    request = urllib.request.Request(f"{LOKI}/loki/api/v1/query_range?{params}",
                                     headers={"Authorization": f"Bearer {helpers.secret('loki.token')}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        streams = json.loads(response.read())["data"]["result"]
    return [json.loads(line) for stream in streams for _, line in stream["values"]]


def main() -> None:
    found = helpers.read_input()
    since, until = found["window"]["since"], found["window"]["until"]
    records = query(since, until)
    denied: Counter[str] = Counter()
    caught: dict[str, dict] = {}
    for record in records:
        if record.get("category") == "authz":
            denied[f"{record.get('role')} {record.get('endpoint')}"] += 1
        elif record.get("category") == "caught" and record.get("exception"):
            caught.setdefault(record["exception"], record)
    previous = (found["state"] or {}).get("denied", {})
    signals = []
    for key, count in sorted(denied.items()):
        before = previous.get(key, 0)
        if count >= SURGE_MINIMUM and count >= SURGE_RATIO * max(before, 1):
            signals.append(helpers.signal(
                key.split(" ", 1)[1], f"{key} 的权限校验失败从 {before} 次增加到 {count} 次",
                [f"窗口 {since} 到 {until}", f"查询 {QUERY}"], f"authz-surge:{key}", severity_hint="P2"))
    for exception, record in sorted(caught.items()):
        signals.append(helpers.signal(
            record.get("message") or exception, f"{exception} 被 catch 后只记了日志，没有向上抛出",
            [helpers.redact(json.dumps(record, ensure_ascii=False))[:500]], f"caught:{exception}"))
    helpers.emit(signals, state={"denied": dict(denied)},
                 notes=[f"读取 {len(records)} 条日志，权限校验失败 {sum(denied.values())} 次"])


if __name__ == "__main__":
    main()
```

## 输入格式

- tightrein 给的输入：`window`、`state`；
- 探针自己读的数据源：日志平台上的规范日志，每行一个 JSON，带 `category`，权限失败带 `role`、`endpoint`，吞掉的
  异常带 `exception`、`message`。

## 输出格式

| 字段 | 取值 |
|---|---|
| `location` | 突增：接口；吞掉的异常：日志消息(没有时为异常类型) |
| `symptom` | 「从 N 次增加到 M 次」或「被 catch 后只记了日志」 |
| `evidence` | 窗口与查询；或脱敏、截断后的日志原文 |
| `severityHint` | 突增 P2；吞掉的异常 null(交给评估判断) |
| `fingerprint` | `authz-surge:<角色> <接口>`、`caught:<异常类型>` |

## 输出文档

运行摘要中的说明(读取条数、失败次数)；运行目录的交接文档与 `-raw/` 下的错误输出。

## 交接内容

突增与吞掉的异常两类信号。评估据此判断：突增是攻击、配置变更还是代码缺陷；吞掉的异常是否让业务静默失败。

## 选用条件

- 日志已送入 Loki(或换成项目所用平台的只读查询 API)，有规范字段；
- 在 secrets.json 写只读令牌 `loki.token`，在登记里写 `secrets: ["loki.token"]`；
- 没有日志规范时先在项目里定规范字段；只想收错误级别的日志用平台错误模块即可，不需要本方法。

## 去重

`state.denied` 记住上次各「角色 接口」的次数，只有加重到阈值才报；吞掉的异常每次运行出现都会报，由去重按指纹
`caught:<异常类型>` 归到同一个问题、累计出现。
