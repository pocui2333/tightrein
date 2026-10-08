# 方法 2：检查定时任务与业务状态

## 介绍

被测系统有必须按时完成的定时任务(每日导入、结算、同步)，或有可以查询的业务状态(待处理的数量、最近一次成功的时间)。
本方法经只读接口或只读查询确认任务按时完成、处理量正常，发现「该做的没做」这类不报错的问题。适用于提供了只读状态
接口(或只读数据库账号)的项目。

## 实现方式

- 调只读接口(如 `GET /api/admin/jobs/import/latest`)取最近一次运行的完成时间与处理量；
- 规则(写在脚本顶部，按项目调整)：
  - 当地时间过了截止时刻(如 02:00)，今天还没有完成 → 「没有按时完成」；
  - 完成了但处理量为 0 → 「处理量为零」；
- 只用接口返回的事实判定，不推测原因。

## 运行模式

- 按 `every` 定时运行(如 `1h`)；与窗口无关，每次查当前状态；
- 只读：只调只读接口，令牌只有读权限；
- 跨次状态：记住上次报过的完成时间(`lastFinishedAt`)，同一次缺失不重复报。

## 架构设计

tightrein 启动脚本，标准输入给输入 JSON(含被测地址 `baseUrl`)；脚本经 `helpers.secret` 取只读令牌、调接口、判定，
经 `helpers.emit` 输出。

## 代码排版

`workspaces/<项目>/scripts/daily_import.py`：常量(截止时刻、时区) → `latest()` 读接口 → `main()` 判定与输出。

```python
"""项目探针 daily-import：每天 02:00 前要完成一次数据导入，检查是否按时完成、处理量是否为零。"""

import json
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tightrein.collect.project_probes import helpers

LOCAL = ZoneInfo("Asia/Shanghai")
DEADLINE_HOUR = 2


def latest(base_url: str, token: str) -> dict:
    request = urllib.request.Request(f"{base_url}/api/admin/jobs/import/latest",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> None:
    found = helpers.read_input()
    job = latest(found["baseUrl"], helpers.secret("demo.readonly"))
    now = datetime.now(timezone.utc).astimezone(LOCAL)
    deadline = now.replace(hour=DEADLINE_HOUR, minute=0, second=0, microsecond=0)
    finished = datetime.fromisoformat(job["finishedAt"]).astimezone(LOCAL) if job.get("finishedAt") else None
    reported = (found["state"] or {}).get("reportedMissing")
    signals = []
    if now >= deadline and (finished is None or finished < deadline.replace(hour=0)):
        if reported != now.date().isoformat():
            signals.append(helpers.signal(
                "job:daily-import", "今天的数据导入没有在 02:00 前完成",
                [f"最近一次完成时间 {finished.isoformat() if finished else '无'}", f"检查时间 {now.isoformat()}"],
                "missed:daily-import", severity_hint="P1"))
            reported = now.date().isoformat()
    elif job.get("processed") == 0:
        signals.append(helpers.signal(
            "job:daily-import", "最近一次数据导入处理了 0 条",
            [f"完成时间 {finished.isoformat()}", "processed=0"], "zero:daily-import", severity_hint="P2"))
    helpers.emit(signals, state={"lastFinishedAt": job.get("finishedAt"), "reportedMissing": reported},
                 notes=[f"最近一次导入处理 {job.get('processed')} 条"])


if __name__ == "__main__":
    main()
```

## 输入格式

- tightrein 给的输入：`baseUrl`(sites.json 的 `target.baseUrl`)、`state`；
- 探针自己读的数据源：只读接口的响应，至少有 `finishedAt`(带时区的时间)与 `processed`(处理条数)。

## 输出格式

| 字段 | 取值 |
|---|---|
| `location` | `job:<任务名>` |
| `symptom` | 「没有在截止时刻前完成」或「处理了 0 条」 |
| `evidence` | 最近一次完成时间、检查时间或处理量 |
| `severityHint` | 没完成 P1，处理量为零 P2 |
| `fingerprint` | `missed:<任务名>`、`zero:<任务名>` |

## 输出文档

运行摘要中的说明(最近一次处理量)；运行目录的交接文档与 `-raw/` 下的错误输出。

## 交接内容

任务没有按时完成或处理量为零的信号。评估据此判断：任务是否真的失败(还是只是晚了)、是代码缺陷还是上游数据没到。

## 选用条件

- 被测系统有只读状态接口(或只读数据库账号)，且有只有读权限的凭据；在 secrets.json 写凭据，在登记里写 `secrets`；
- 在 sites.json 写 `target.baseUrl`；
- 没有只读接口时，先在项目里加一个(只返回状态，不带业务数据)，或改用方法 1(项目自己写运行记录)。

## 去重

`state.reportedMissing` 记住已报过缺失的日期，同一天不重复报；第二天仍缺失会再报，去重按指纹归到同一个问题、累计出现。
处理量为零每次运行都会报，靠去重的指纹归并累计。
