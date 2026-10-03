-- server_log_cursors 改为按日志来源保存扩展返回的读取位置与解析状态(architecture/01 4.2，architecture/10 3.4、3.5)。
-- 旧表按日志文件记录偏移量，读取位置的含义由核心决定；现在读取位置由 log-source 扩展给出，核心原样保存、不解读，
-- 旧记录无法换算为扩展的读取位置，删除后重建，下一次读取按首次读取处理。

DROP TABLE server_log_cursors;

CREATE TABLE server_log_cursors (
    source TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    parse_state TEXT,
    updated_at TEXT NOT NULL
) STRICT;
