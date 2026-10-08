-- 第 1 版表结构(store/README.md)。之后的改动写成 migrations/<三位序号>_<名称>.sql，从 002 起编号。
-- 列名小写加下划线；时间一律 ISO 8601 UTC 文本(2026-10-07T09:30:00Z)；偶尔用到的字段放进 extra(JSON 文本)。
-- 状态等取值不用 CHECK 写死：取值由各阶段的状态机校验，SQLite 改 CHECK 要重建整张表。
-- STRICT：列类型写错即报错，不悄悄存成别的类型。

CREATE TABLE problems (
    id TEXT PRIMARY KEY,                 -- P-0001
    fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,                -- 采集模块：collect.platform_errors 等
    check_type TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    last_commit TEXT,
    issue TEXT,
    muted_until TEXT,
    extra TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- 去重按指纹查找
CREATE INDEX problems_fingerprint ON problems (fingerprint);

CREATE TABLE occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
    run TEXT,                            -- 不设外键：运行记录 90 天后清理，出现记录保留
    seen_at TEXT NOT NULL,
    "commit" TEXT,
    evidence TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- 验收与评估按问题取出现记录
CREATE INDEX occurrences_problem ON occurrences (problem, seen_at);

CREATE TABLE issues (
    id TEXT PRIMARY KEY,                 -- 0018
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT,
    kind TEXT NOT NULL,                  -- bug、feature
    origin TEXT NOT NULL,                -- problem、user
    gate TEXT,
    stage TEXT,
    step TEXT,
    round INTEGER,
    branch TEXT,
    pr INTEGER,
    merge_commit TEXT,
    deploy TEXT,
    held_by TEXT,
    extra TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- watch 与 status 按状态列出
CREATE INDEX issues_status ON issues (status);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,                 -- R-20261007T093000Z-collect
    stage TEXT NOT NULL,
    "trigger" TEXT,                      -- schedule、event、manual；从文件重建的运行不知道触发方式，为空
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    heartbeat_at TEXT,
    holder_pid INTEGER,
    holder_host TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- 启动恢复找进行中的运行，status 命令找各阶段最近一次运行
CREATE INDEX runs_status ON runs (status);
CREATE INDEX runs_stage_started ON runs (stage, started_at);

CREATE TABLE operations (
    "key" TEXT PRIMARY KEY,              -- 对象 + 步骤 + 操作 + 内容哈希
    status TEXT NOT NULL,                -- in_progress、done
    result TEXT,
    subject TEXT,
    point TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE counters (
    "key" TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0,
    window_start TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE state (
    "key" TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE sequences (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;
