-- Issue 状态收敛为六种(redesign/04-issue.md)：待决定、待修、进行中、待合并、完成、取消。进行中的细分与完成后等待
-- 部署后确认记在 phase；拆分出的子任务记父 Issue(parent)；来源(source)写进头信息。映射与 domain/issue.legacy_status
-- 相同：带 hold 的未关闭 Issue 为待决定；已合并为完成(关闭原因 fixed，phase deploy-check)；已关闭按关闭原因分为完成与取消。
-- issue_events 的状态按同一映射改写(重新打开的事件不带关闭原因，来源状态记为取消)。
-- github_mirror.labelled_status 保留旧值，下次对齐据此去掉旧标签；relations 记下已建立的子 Issue 与阻塞关系。

ALTER TABLE issues ADD COLUMN phase TEXT;
ALTER TABLE issues ADD COLUMN parent TEXT;
ALTER TABLE issues ADD COLUMN source TEXT;
ALTER TABLE github_mirror ADD COLUMN relations TEXT;

UPDATE issues SET phase = CASE status
    WHEN 'fixing' THEN 'fix'
    WHEN 'to-verify' THEN 'verify'
    WHEN 'to-submit' THEN 'submit'
    WHEN 'merged' THEN 'deploy-check'
END;
UPDATE issues SET close_reason = 'fixed' WHERE status = 'merged';
UPDATE issues SET status = CASE
    WHEN status = 'closed' AND close_reason = 'fixed' THEN 'done'
    WHEN status = 'closed' THEN 'cancelled'
    WHEN status = 'merged' THEN 'done'
    WHEN hold IS NOT NULL THEN 'needs-decision'
    WHEN status = 'in-review' THEN 'needs-decision'
    WHEN status IN ('fixing', 'to-verify', 'to-submit') THEN 'in-progress'
    WHEN status = 'pr-review' THEN 'pending-merge'
    ELSE status
END;
UPDATE issues SET phase = NULL WHERE status = 'needs-decision';
UPDATE issues SET hold = NULL WHERE status <> 'needs-decision';

UPDATE issue_events SET from_status = CASE
    WHEN from_status = 'closed' AND close_reason = 'fixed' THEN 'done'
    WHEN from_status = 'closed' THEN 'cancelled'
    WHEN from_status = 'merged' THEN 'done'
    WHEN from_status = 'in-review' THEN 'needs-decision'
    WHEN from_status IN ('fixing', 'to-verify', 'to-submit') THEN 'in-progress'
    WHEN from_status = 'pr-review' THEN 'pending-merge'
    ELSE from_status
END;
UPDATE issue_events SET to_status = CASE
    WHEN to_status = 'closed' AND close_reason = 'fixed' THEN 'done'
    WHEN to_status = 'closed' THEN 'cancelled'
    WHEN to_status = 'merged' THEN 'done'
    WHEN to_status = 'in-review' THEN 'needs-decision'
    WHEN to_status IN ('fixing', 'to-verify', 'to-submit') THEN 'in-progress'
    WHEN to_status = 'pr-review' THEN 'pending-merge'
    ELSE to_status
END;
