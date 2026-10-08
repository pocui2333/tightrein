# 方法 1：读项目自己的日志文件

## 介绍

服务前面的 nginx(或应用自己)把每个请求写进本机的 access.log，没有送进日志平台。本方法按时间窗口从日志文件里取出
窗口内的行交给 tightrein，由 tightrein 统计并与基线比较。适用于 tightrein 能读到日志文件(同一台机器或挂载目录)的项目。

## 实现方式

- 按行读日志文件(含轮转出的上一份，如 `access.log.1`)，解析每行的时间，取落在 `[since, until)` 内的行；
- 不在脚本里判定退化：原文交给 tightrein，按 `controls."collect.access_log".pattern`(纯文本)或 `fields`(JSON 行)解析；
- 读满 `limit` 行就停，`truncated` 为真。

## 运行模式

- 按 `schedule.every."collect.access_log"`(缺省每天)运行，窗口为上次终点到现在，第一次回看 `lookback`；
- 只读：不移动、不截断、不轮转日志文件；
- 不需要跨次状态：读取位置由 tightrein 保存。

## 架构设计

tightrein 的 `collect/access_log/source.py` 启动脚本，标准输入给窗口；脚本只负责取行，标准输出交回 `lines`；解析、统计、
基线比较在 tightrein 里。

## 代码排版

`workspaces/<项目>/scripts/access_from_file.py`：常量(文件路径、时间写法) → `in_window()` 按时间筛行 → `main()` 输出。

```python
"""访问日志取数：从 nginx 的 access.log 取窗口内的行(只读)。"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

FILES = [Path("/var/log/nginx/access.log.1"), Path("/var/log/nginx/access.log")]   # 先旧后新
TIME = re.compile(r"\[(?P<time>[^\]]+)\]")
TIME_FORMAT = "%d/%b/%Y:%H:%M:%S %z"


def main() -> None:
    found = json.load(sys.stdin)
    since = datetime.fromisoformat(found["window"]["since"].replace("Z", "+00:00"))
    until = datetime.fromisoformat(found["window"]["until"].replace("Z", "+00:00"))
    lines, truncated = [], False
    for path in FILES:
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                match = TIME.search(line)
                if match is None or not since <= datetime.strptime(match["time"], TIME_FORMAT) < until:
                    continue
                if len(lines) >= found["limit"]:
                    truncated = True
                    break
                lines.append(line.rstrip("\n"))
    json.dump({"lines": lines, "truncated": truncated}, sys.stdout)


if __name__ == "__main__":
    main()
```

`pattern`(写在 `controls."collect.access_log".pattern`)按 nginx 的 `log_format` 写，耗时要是毫秒：`$request_time` 是秒，
日志格式里改记毫秒，或在脚本里换算后改交 `requests`(见方法 2 的输出格式)。

## 输入格式

- tightrein 给的输入：`window`、`limit`、`workspace`；
- 脚本自己读的数据源：日志文件，每行一个请求，带时间。

## 输出格式

`{"lines": [...], "truncated": false}`，见 `output.schema.json`。

## 输出文档

运行目录的 `13-collect.access_log-handoff.json`(退化信号与统计)；脚本的错误输出脱敏后在 `13-collect.access_log-raw/`。

## 交接内容

交给 tightrein 的是窗口内的原文行；tightrein 交给去重的是每个退化接口一条信号，评估据此判断退化是否由代码改动引起。

## 选用条件

- tightrein 能只读访问日志文件；日志每行带时间、方法、路由、状态码(最好带耗时)；
- 日志在别的机器上且进不了日志平台时，先把日志送进日志平台，用日志平台取法。

## 去重

无：每个窗口只读一次，退化由 tightrein 与基线比较得出；同一接口的退化由去重按指纹归到同一个问题。
