-- 拆分出的后续子任务(architecture/07 4.6)：前一个子任务的 Issue 编号，它合并后才可开始；与 Issue 文件 frontmatter 的
-- dependsOn 一致。已有的 Issue 都不依赖其他 Issue。

ALTER TABLE issues ADD COLUMN depends_on TEXT;
