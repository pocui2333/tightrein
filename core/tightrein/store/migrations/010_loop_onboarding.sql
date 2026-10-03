-- 验证、编排与接入：
-- 修复前复现只在修复内部做，verify 的 reproduce 阶段删除；旧的 reproduce 交接记录一并删除(文件保留，只作历史)。
-- workspace_meta：工作区的阶段(phase：onboarding 接入中、running 运行中)与暂停标记(paused)；已有工作区视为运行中。
-- breaker_counts：无人值守推进中各对象的连续失败与无进展次数(熔断)。
-- onboarding_items：接入清单各项的状态、推荐答案与用户的回答。

DELETE FROM handoffs WHERE stage = 'verify' AND phase = 'reproduce';

CREATE TABLE workspace_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

INSERT INTO workspace_meta (key, value, updated_at) VALUES ('phase', 'running', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

CREATE TABLE breaker_counts (
    subject_id TEXT PRIMARY KEY,
    step TEXT,
    failures INTEGER NOT NULL,
    repeats INTEGER NOT NULL,
    last_reason TEXT,
    tripped_at TEXT,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE onboarding_items (
    item TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    owner TEXT NOT NULL,
    detail TEXT NOT NULL,
    recommendation TEXT,
    answer TEXT,
    updated_at TEXT NOT NULL
) STRICT;
