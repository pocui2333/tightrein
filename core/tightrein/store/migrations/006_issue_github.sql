-- Issue 的 GitHub 镜像(architecture/06 10.8)：issues.tracker 为 github 时本地 Issue 在 GitHub 上的编号与链接，
-- 与 Issue 文件 frontmatter 的 github 一致；镜像的簿记不属于 Issue，单独成表，reindex 不改动。
-- github_mirror：remote_state 为 GitHub 上已知的开关状态(open、closed)，labelled_status 为已打的状态标签对应的本地状态，
-- error、failed_at 为最近一次失败(成功后清空)。github_mirror_comments：关键节点的待发评论，posted_at 为发出时间。

ALTER TABLE issues ADD COLUMN github_number INTEGER;
ALTER TABLE issues ADD COLUMN github_url TEXT;

CREATE TABLE github_mirror (
    issue_id TEXT PRIMARY KEY,
    remote_state TEXT,
    labelled_status TEXT,
    error TEXT,
    failed_at TEXT,
    synced_at TEXT
) STRICT;

CREATE TABLE github_mirror_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    posted_at TEXT
) STRICT;
CREATE INDEX github_mirror_comments_issue ON github_mirror_comments (issue_id, posted_at);
