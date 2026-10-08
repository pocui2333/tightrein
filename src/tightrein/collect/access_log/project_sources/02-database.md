# 方法 2：查项目自己的数据库

## 介绍

项目把每个请求记进自己的数据库(访问记录表、审计表)，没有访问日志文件可读。本方法用只读账号按时间窗口查出请求，
已解析好交给 tightrein。适用于有访问记录表、且能开只读账号的项目。

## 实现方式

- 用只读账号查询窗口内的记录：`SELECT method, route, status, duration_ms FROM access_records WHERE created_at >= ? AND
  created_at < ? ORDER BY created_at LIMIT ?`(表名、列名按项目改)；
- route 应是路由模板(如 `/api/orders/{id}`)而不是带编号的实际路径，否则同一个接口会拆成很多个；表里只有实际路径时
  在 SQL 或脚本里归一；
- 查出 `limit` 条时 `truncated` 为真。

## 运行模式

- 按 `schedule.every."collect.access_log"` 运行，窗口为上次终点到现在；
- 只读：只读账号，只执行 SELECT；
- 不需要跨次状态。

## 架构设计

tightrein 启动脚本，标准输入给窗口与条数上限；脚本从 `TIGHTREIN_SECRET_<条目>` 环境变量取连接串(接入清单里登记的凭据)，
查库后标准输出交回 `requests`。

## 代码排版

`workspaces/<项目>/scripts/access_from_db.py`：常量(SQL) → `main()` 查询与输出。

```python
"""访问日志取数：从项目数据库的访问记录表查窗口内的请求(只读账号)。"""

import json
import os
import sqlite3   # 换成项目所用数据库的驱动
import sys

SQL = ("SELECT method, route, status, duration_ms FROM access_records "
       "WHERE created_at >= ? AND created_at < ? ORDER BY created_at LIMIT ?")


def main() -> None:
    found = json.load(sys.stdin)
    connection = sqlite3.connect(f"file:{os.environ['TIGHTREIN_SECRET_DEMO_ACCESS_DB']}?mode=ro", uri=True)
    rows = connection.execute(SQL, (found["window"]["since"], found["window"]["until"], found["limit"])).fetchall()
    requests = [{"method": method, "route": route, "status": status, "durationMs": duration}
                for method, route, status, duration in rows]
    json.dump({"requests": requests, "truncated": len(rows) >= found["limit"]}, sys.stdout)


if __name__ == "__main__":
    main()
```

## 输入格式

- tightrein 给的输入：`window`、`limit`、`workspace`；凭据经环境变量 `TIGHTREIN_SECRET_<条目名大写，非字母数字换成 _>`；
- 脚本自己读的数据源：访问记录表。

## 输出格式

`{"requests": [{"method", "route", "status", "durationMs"}], "truncated": bool}`，见 `output.schema.json`。

## 输出文档

运行目录的 `13-collect.access_log-handoff.json`；脚本的错误输出脱敏后在 `13-collect.access_log-raw/`。

## 交接内容

已解析的请求；tightrein 统计、与基线比较后每个退化接口一条信号，评估据此判断退化是否由代码改动引起。

## 选用条件

- 有访问记录表，记录了方法、路由(最好是模板)、状态码、耗时与时间；
- 能开只读账号，连接串写进 secrets.json，接入清单的 `secrets` 登记条目名；
- 没有访问记录表时用方法 1(日志文件)或日志平台取法。

## 去重

无：每个窗口只查一次；同一接口的退化由去重按指纹归到同一个问题。
