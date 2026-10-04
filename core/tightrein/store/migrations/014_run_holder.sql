-- 运行记录开始它的进程号与主机：中断识别据此认出没有持有对象锁、进程已不存在的运行(orchestrator/recovery)。
-- 迁移前的行两列为空，中断识别对它们沿用对象锁的规则。

ALTER TABLE runs ADD COLUMN holder_pid INTEGER;
ALTER TABLE runs ADD COLUMN holder_host TEXT;
