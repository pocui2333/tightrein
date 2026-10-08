# store：存储

## 是什么

所有读写(数据库、文件、锁、幂等键、预算与熔断计数)的唯一入口。原则是**文件存内容，数据库只存查询与状态**：每一步的内容已按对象归档成文件(`protocol/naming.md`)，数据库不存第二份，因此除计数外都能从文件重建。协议层与各阶段只调用这里，不自己拼路径、不自己写 SQL 以外的存储。

## 流程

1. 入口用 `db.open_database(layout.database)` 打开工作区数据库(WAL、busy_timeout 5000ms、外键、自动提交)，同时执行尚未执行的迁移；一个进程一个连接。
2. 运行开始：`locks.FileLock(layout.run_lock, clock, stale_s=…)` 取运行锁，`tables.runs.free_id` 取编号、`runs.start` 登记，按保留期 `retention.purge` 清理一次。
3. 运行中：每隔心跳间隔 `lock.beat()` 与 `runs.heartbeat`；对外写操作经 `tables.operations.run_once`；编号经 `tables.sequences.next_value`；多条语句要原子时用 `db.transaction`。
4. 运行结束：`runs.finish`，`lock.release()`(用 `with` 时出错也会释放)。
5. 数据库坏了或升级：`rebuild.rebuild(layout, conn)` 从文件重建(`tightrein admin rebuild`)。

## 输入与输出

### 表(schema.sql，第 1 版)

列名小写下划线(程序中 dataclass 的属性同名，写进 JSON 时才转小驼峰)；时间一律 ISO 8601 UTC 文本；每张表都有 `created_at`、`updated_at`(由 `tables/table.py` 维护，不在记录中)；偶尔用的字段放 `extra` JSON 列。

| 表 | 主键 | 用途 | 能否从文件重建 |
|---|---|---|---|
| problems | `P-0001` | 去重后的问题；按 fingerprint 建索引 | 能：由去重模块登记重建函数(第 2 批) |
| occurrences | 自增 | 问题的每次出现；随问题级联删除 | 能：随 problems 一起 |
| issues | `0018` | Issue 的状态、所在步骤、分支、PR、合并、部署；按 status 建索引 | 能：由评估模块登记重建函数(第 3 批) |
| runs | `R-20261007T093000Z-collect` | 运行索引、心跳与持有者 | 能：本模块的 `rebuild_runs` |
| operations | 幂等键 | 对外写操作的进行中 / 已完成与结果 | 不能(没有对应文件)，重建时保留 |
| counters | 键 | 预算用量与熔断计数 | 不能，重建时保留 |
| state | 键 | 各来源的读取位置、上次的哈希与时间 | 不能，重建时保留 |
| sequences | 序列名 | 编号分配 | 重建后按表中最大编号推进 |

状态等取值不用 CHECK 写死：取值由各阶段的状态机校验(44c)，SQLite 改 CHECK 要重建整张表。

### 文件

| 文件 | 管什么 |
|---|---|
| `files/layout.py` | 全部路径的唯一计算处；路径段经 `naming.segment` 校验，含 `/`、`\`、`\0` 或为 `.`、`..` 即拒绝 |
| `files/atomic.py` | 同目录临时文件(名字带进程号)写完再 `os.replace`，失败时目标文件保持原样 |
| `files/json.py` | UTF-8、两格缩进、LF、末尾空行 |
| `files/markdown.py` | UTF-8、LF、末尾一个空行；`demote_headings` 把一份文档的标题整体下移，嵌进另一份文档 |
| `files/directories.py` | 逐个列举对象目录、运行目录(保留期清理与重建共用) |

### 锁文件

`data/run.lock`(运行锁)、`data/locks/<编号>.lock`(对象锁)，内容是持有者 JSON：`{"pid", "host", "acquiredAt", "heartbeatAt"}`。`acquire` 返回被接管的旧持有者(调用方记一条事件)；被占且未失效时 `wait=False` 抛 `Busy`；`beat` 发现已被接管时抛 `Lost`。

### 重建 runs 的约定

运行目录名给出编号、阶段、开始时间；状态与结束时间取自引用该运行的各 `*-handoff.json`(运行目录与 Issue、问题目录中)：有失败步骤为 failed，否则有步骤为 done，没有步骤为 interrupted；结束时间取最晚一份交接的 `createdAt`。触发方式没有写进文件，重建后为空。

## 配置

| 用途 | settings 中的键 | 缺省 |
|---|---|---|
| 锁的心跳与失效 | `limits.lock.heartbeat`、`limits.lock.stale` | 30s、90s |
| 保留期 | `records.retention.runs`、`records.retention.raw` | 90d、30d |
| 幂等键保留期 | `purge` 的 `operations` 键(recovery.md 定为 30 天) | 30d |

`retention.purge` 的 policy 以秒为单位，由调用方从 settings 换算(`naming.parse_duration`)；没给的键不清理。busy_timeout(5000ms)写死在 `db.py`。

## 设计依据

- **WAL 且切换失败即报错**：读者不被写事务阻塞；网络文件系统等不支持 WAL 时报错，不悄悄退回回滚日志模式。出处：SQLite WAL 文档(sqlite.org/wal.html)。
- **事务最外层 `BEGIN IMMEDIATE`，嵌套用 SAVEPOINT**：开始即取写锁，避免两个事务先读后写、升级写锁时互相等待而死锁(SQLITE_BUSY 无法靠 busy_timeout 解开)；内层出错只回滚内层。出处：sqlite.org/lang_transaction.html。
- **schema.sql 为第 1 版，之后编号迁移**：旧的 14 个迁移不保留。迁移文件名 `<三位序号>_<名称>.sql`，名字不合规或序号重复即报错；每个迁移连同执行记录在一个事务中，失败整体回滚；数据库中有程序不认识的版本或同号改名即拒绝打开；不能在事务内迁移(`executescript` 会先提交未完成的事务)。
- **编号用一条 `INSERT … ON CONFLICT DO UPDATE … RETURNING`**：并发不重号，在外层事务中分配的随回滚撤销；重建后 `ensure_at_least` 只前进不后退。
- **幂等键**：执行前写进行中，成功后存结果，再次执行直接返回；普通异常删键允许重试；中断(`Interrupted`、`KeyboardInterrupt`)或进程被杀时键停在进行中，再次执行抛 `InProgress`，先到远端核对再 `complete` 或 `abandon`。出处：Stripe 幂等键(stripe.com/docs/api/idempotent_requests)，保留期按 recovery.md 定为 30 天。
- **文件锁加心跳**：持有者以锁文件内容为准，flock 只包住「读 → 判断 → 写」一小段。不让 flock 覆盖整个持有期，是因为卡死的进程仍握着 flock，别人永远接管不了；按心跳最多 90 秒即可接管(原来按 2 小时过期)。同主机持有进程已不存在时立即判失效；其他主机只按心跳。锁文件原地改写，不用临时文件加改名(改名会让别的进程的 flock 落在旧文件上)。出处：Temporal 活动心跳(temporal.io/blog/activity-timeouts)。
- **保留期的年龄取自运行编号**：复制、解压、改动都会改掉文件修改时间；Issue、问题目录中的 prompt、raw 按同一步 `handoff.json` 记下的运行编号判断，找不到就不删。先在一个事务中删数据库记录，成功后再删文件。出处：GitHub Actions 日志缺省 90 天。
- **原子写的临时文件带进程号**：多个进程同写一个目标时不互相覆盖临时文件；finally 中清掉临时文件。

## 不做什么

- 不保留旧的 38 张表与 `repos/`：信号、分诊结果、提案、建议、知识库元数据等内容都在文件里；事件写进 `events.jsonl`，不再建事件表。
- 不建 `locks` 表：对象锁与运行锁都是文件锁。
- 不镜像 GitHub：Issue 只在状态变化时同步。
- 不用 CHECK 约束写死状态取值(见上)。
- launchd 日志轮转不在这里，在 `protocol/schedule/`。
- 不用文件修改时间判断年龄(见设计依据)。
