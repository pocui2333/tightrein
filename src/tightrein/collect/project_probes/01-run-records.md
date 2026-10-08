# 方法 1：读取运行记录(JSON Lines)

## 介绍

项目自己的定时流程(采集、导入、同步、报表)每跑一次在本机写一行运行记录(JSON Lines)，记下状态与问题。本方法把
窗口内失败与带警告的运行转成信号，发现「流程跑了但没跑好」。适用于在本机或可读目录里留有运行记录文件的项目。

## 实现方式

- 读运行记录文件，逐行解析 JSON，取运行时间落在输入窗口 `[since, until)` 内的记录；
- 状态为正常的跳过；失败、有警告的每条记录转成信号：记录里带问题清单(`issues`)时每个问题一条，没有时整条运行一条；
- 严重度按状态映射：失败 → P1，有警告 → P3(映射表写在脚本顶部，按项目调整)；
- 记录的时间没有时区时按项目所在时区解释，再换成 UTC 写进 `occurredAt`。

## 运行模式

- 按登记的 `every` 定时运行(如 `1h`)，窗口为上次成功运行到现在，第一次回看 `lookback`(缺省 24 小时)；
- 只读：只读记录文件，不移动、不截断；
- 不需要跨次状态(窗口不重叠，同一条记录只会被读一次)，`state` 为 null。

## 架构设计

tightrein 的 `collect/project_probes/source.py` 按登记启动脚本，标准输入给输入 JSON；脚本读记录文件、判定、经
`helpers.emit` 把输出 JSON 写到标准输出；tightrein 校验输出后转成信号交给去重。

## 代码排版

`workspaces/<项目>/scripts/run_records.py`，一个文件：

1. 常量：记录文件路径、时区、状态 → 严重度；
2. `main()`：`helpers.read_input()` 取窗口；
3. 逐行读取、按窗口筛选；
4. 判定并组装信号(`helpers.signal`)；
5. `helpers.emit(signals, notes=[...])`。

```python
"""项目探针 run-records：读取本机流程写的 runs.jsonl，把失败与带警告的运行转成信号(只读)。"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tightrein.collect.project_probes import helpers

LOG = Path("/path/to/project/data/logs/runs.jsonl")   # 探针在工作区根目录运行，写绝对路径
LOCAL = ZoneInfo("Asia/Shanghai")
SEVERITY = {"失败": "P1", "有警告": "P3"}
NORMAL = "正常"


def main() -> None:
    found = helpers.read_input()
    since = datetime.fromisoformat(found["window"]["since"].replace("Z", "+00:00"))
    until = datetime.fromisoformat(found["window"]["until"].replace("Z", "+00:00"))
    signals, read = [], 0
    for line in LOG.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        at = datetime.strptime(record["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL)
        if not since <= at < until:
            continue
        read += 1
        if record["status"] == NORMAL:
            continue
        issues = record.get("issues") or [{"kind": "run-status", "text": record["summary"]}]
        for issue in issues:
            signals.append(helpers.signal(
                f"{record['section']}:{issue['kind']}", issue["text"],
                [f"运行 {record['run']}({record['at']})：{record['status']}，{record['summary']}"],
                f"{record['section']}:{issue['kind']}", severity_hint=SEVERITY.get(record["status"]),
                occurred_at=at.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
                context={"stats": record.get("stats")}))
    helpers.emit(signals, notes=[f"读取 {read} 条运行记录"])


if __name__ == "__main__":
    main()
```

## 输入格式

- tightrein 给的输入：`window.since`、`window.until`(其余字段见 `README.md` 的「契约」)；
- 探针自己读的数据源：运行记录文件，每行一个 JSON，至少有 `at`(时间)、`section`(流程段)、`status`、`summary`、
  `run`(运行编号)，可选 `issues`(`[{kind, text}]`)、`stats`。字段名不同时改脚本里的取值。

## 输出格式

| 字段 | 取值 |
|---|---|
| `location` | `<流程段>:<问题种类>`，如 `collect:missing-data` |
| `symptom` | 问题原文；没有问题清单时为运行摘要 |
| `evidence` | 一条：运行编号、时间、状态与摘要 |
| `severityHint` | 按状态映射，映射不到为 null |
| `fingerprint` | 与 location 相同：同一流程段的同一种问题每次相同 |
| `occurredAt` | 运行时间(UTC) |
| `context` | 运行的统计数据 |

## 输出文档

- 运行摘要中的说明「读取 N 条运行记录」；
- 运行目录的 `11-collect.project_probes-handoff.json`(本次信号)与 `11-collect.project_probes-raw/<探针名>.stderr.log`
  (有错误输出时，已脱敏)。

## 交接内容

每条失败或带警告的运行问题一条信号。评估据此判断：是项目代码的缺陷，还是外部原因(数据源不可用、网络)；同一问题
是否反复出现。

## 选用条件

- 项目在 tightrein 能读到的位置写运行记录(本机文件)，且记录带状态与时间；
- 没有运行记录时，先让项目把每次运行的状态写进 JSON Lines，或改用方法 2(查只读状态接口)。

## 去重

窗口不重叠，同一条记录只报一次；同一问题再次发生(新的运行记录)会再报一条，由去重按指纹归到同一个问题、累计出现。
不需要 `state`。
