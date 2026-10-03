-- 采集的新来源：
-- 读取位置按来源保存时间窗口的终点：server_log_cursors 改名 source_cursors，旧的日志文件位置无法换算，清空后按首次读取处理。
-- server-log 由 platform-errors 取代：运行、信号与问题的采集方法改写，问题范围的来源记为 log-platform。
-- e2e 不再采集：e2e 的运行与信号删除；没有 Issue 的 e2e 问题删除，有 Issue 的改为 incidental(不再被覆盖运行判为已解决)。
-- 问题状态只保留待确认、新、持续、已解决、回归、已忽略：不稳定改为待确认并标记间歇；环境问题删除。
-- 环境判定与跨探针关联删除：待复核的信号作废，problem_links 表与 runs 的 environment_verdict、companion_run_id 列删除。
-- 新表：pending_claims(静态巡检未取证的疑点)、probe_states(项目探针的上次运行时间与状态)。

ALTER TABLE server_log_cursors RENAME TO source_cursors;
DELETE FROM source_cursors;

UPDATE runs SET probe = 'platform-errors' WHERE probe = 'server-log';
UPDATE signals SET probe = 'platform-errors' WHERE probe = 'server-log';
UPDATE problems SET probe = 'platform-errors' WHERE probe = 'server-log';

DELETE FROM triage_results WHERE problem_id IN (SELECT id FROM problems WHERE probe = 'e2e' AND issue_id IS NULL);
DELETE FROM problems WHERE probe = 'e2e' AND issue_id IS NULL;
UPDATE problems SET probe = 'incidental' WHERE probe = 'e2e';
DELETE FROM problem_signals WHERE signal_id IN (SELECT id FROM signals WHERE probe = 'e2e');
DELETE FROM signals WHERE probe = 'e2e';
DELETE FROM runs WHERE probe = 'e2e';

DELETE FROM triage_results WHERE problem_id IN (
    SELECT id FROM problems WHERE status = 'environment'
    OR id IN (SELECT problem_id FROM problem_events WHERE event = 'environment-detected'));
DELETE FROM problems WHERE status = 'environment'
    OR id IN (SELECT problem_id FROM problem_events WHERE event = 'environment-detected');
UPDATE problems SET status = 'pending', intermittent = 1 WHERE status = 'flaky';
UPDATE problem_events SET from_status = 'pending' WHERE from_status = 'flaky';
UPDATE problem_events SET to_status = 'pending' WHERE to_status = 'flaky';

UPDATE problems SET scope = json_object(
    'location', json_extract(scope, '$.location'),
    'roles', json(coalesce(json_extract(scope, '$.roles'), '[]')),
    'source', CASE probe WHEN 'platform-errors' THEN 'log-platform' ELSE NULL END);

UPDATE runs SET coverage = CASE
    WHEN json_extract(coverage, '$.serverLog') THEN
        json_set(json_remove(coverage, '$.pages', '$.pagesTotal', '$.cases', '$.serverLog'), '$.sources',
                 json('["log-platform"]'))
    ELSE json_remove(coverage, '$.pages', '$.pagesTotal', '$.cases', '$.serverLog') END;

UPDATE signals SET aggregate_state = 'voided' WHERE aggregate_state = 'held';

DROP TABLE problem_links;
ALTER TABLE runs DROP COLUMN environment_verdict;
ALTER TABLE runs DROP COLUMN companion_run_id;

CREATE TABLE pending_claims (
    id TEXT PRIMARY KEY,
    claim TEXT NOT NULL,
    severity TEXT,
    reason TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL
) STRICT;
CREATE INDEX pending_claims_state ON pending_claims (state, reason);

CREATE TABLE probe_states (
    name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    state TEXT
) STRICT;
