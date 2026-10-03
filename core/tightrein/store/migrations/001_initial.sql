-- 初始结构(architecture/01 4.2)。时间为 UTC ISO 8601 文本(format_iso)，日期为 YYYY-MM-DD，JSON 列为文本，布尔为 0 或 1。
-- 外键只用于随问题或信号一起删除的从属记录；运行、Issue 等跨模块引用不加外键，避免写入顺序依赖。

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE sequences (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
) STRICT;

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    probe TEXT,
    level TEXT,
    parent_run_id TEXT,
    companion_run_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    target_commit TEXT,
    coverage TEXT NOT NULL,
    environment_verdict TEXT,
    environment_detail TEXT NOT NULL,
    status TEXT NOT NULL,
    aggregated_at TEXT,
    trace_id TEXT
) STRICT;
CREATE INDEX runs_stage_started ON runs (stage, started_at);
CREATE INDEX runs_aggregated ON runs (aggregated_at);

CREATE TABLE signals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    probe TEXT NOT NULL,
    "check" TEXT NOT NULL,
    environment TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    release TEXT,
    location TEXT NOT NULL,
    message TEXT NOT NULL,
    normalized_message TEXT,
    context TEXT NOT NULL,
    actor TEXT NOT NULL,
    fingerprint TEXT,
    suppressed INTEGER NOT NULL,
    aggregate_state TEXT NOT NULL
) STRICT;
CREATE INDEX signals_fingerprint ON signals (fingerprint);
CREATE INDEX signals_run ON signals (run_id);
CREATE INDEX signals_occurred ON signals (occurred_at);
CREATE INDEX signals_aggregate_state ON signals (aggregate_state);

CREATE TABLE problems (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    fingerprint_version INTEGER NOT NULL,
    probe TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    first_seen_release TEXT,
    last_seen_release TEXT,
    resolved_release TEXT,
    occurrences INTEGER NOT NULL,
    issue_id TEXT,
    ignore_until TEXT,
    intermittent INTEGER NOT NULL,
    clean_covered_runs INTEGER NOT NULL,
    merged_into TEXT REFERENCES problems (id) ON DELETE CASCADE,
    scope TEXT NOT NULL
) STRICT;
CREATE INDEX problems_status ON problems (status);
CREATE INDEX problems_merged_into ON problems (merged_into);

CREATE TABLE problem_signals (
    problem_id TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
    signal_id TEXT NOT NULL REFERENCES signals (id) ON DELETE CASCADE,
    PRIMARY KEY (problem_id, signal_id)
) STRICT;
CREATE INDEX problem_signals_signal ON problem_signals (signal_id);

CREATE TABLE problem_aliases (
    fingerprint TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX problem_aliases_problem ON problem_aliases (problem_id);

CREATE TABLE problem_links (
    problem_a TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
    problem_b TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (problem_a, problem_b, kind)
) STRICT;
CREATE INDEX problem_links_b ON problem_links (problem_b);

CREATE TABLE problem_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    run_id TEXT,
    operation TEXT NOT NULL,
    reason TEXT,
    detail TEXT NOT NULL,
    handled_at TEXT
) STRICT;
CREATE INDEX problem_events_problem ON problem_events (problem_id, at);
CREATE INDEX problem_events_event ON problem_events (event, handled_at);

CREATE TABLE triage_results (
    problem_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    severity TEXT,
    fixability TEXT,
    complexity TEXT,
    root_causes TEXT NOT NULL,
    introduced_by TEXT,
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    triage_commit TEXT NOT NULL,
    refuter_verdict TEXT,
    priority_score REAL,
    flags TEXT NOT NULL,
    labels TEXT NOT NULL,
    outcome TEXT,
    outcome_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (problem_id, attempt)
) STRICT;
CREATE INDEX triage_results_disposition ON triage_results (disposition);
CREATE INDEX triage_results_outcome_at ON triage_results (outcome_at);

CREATE TABLE issues (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    close_reason TEXT,
    severity TEXT NOT NULL,
    fixability TEXT,
    priority_score REAL,
    problems TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    introduced_by TEXT,
    triage_commit TEXT,
    findings TEXT,
    branch TEXT,
    pr TEXT,
    hold TEXT,
    file_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
CREATE INDEX issues_status ON issues (status);

CREATE TABLE issue_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    close_reason TEXT,
    actor TEXT NOT NULL,
    note TEXT
) STRICT;
CREATE INDEX issue_events_issue ON issue_events (issue_id, at);

CREATE TABLE handoffs (
    stage TEXT NOT NULL,
    phase TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    stale_at TEXT,
    PRIMARY KEY (stage, phase, subject_id, attempt)
) STRICT;
CREATE INDEX handoffs_run ON handoffs (run_id);

CREATE TABLE scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    run_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    item TEXT NOT NULL,
    result TEXT NOT NULL,
    method TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX scores_stage_run ON scores (stage, run_id);
CREATE INDEX scores_item ON scores (item);

CREATE TABLE failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    run_id TEXT,
    span_ids TEXT NOT NULL,
    stage TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    reusable INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_type, source_id)
) STRICT;
CREATE INDEX failures_stage_category ON failures (stage, category);

CREATE TABLE proposals (
    id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    evidence TEXT NOT NULL,
    changes TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    patch_path TEXT,
    expected_effect TEXT NOT NULL,
    eval_report TEXT,
    reject_reason TEXT,
    merged_commit TEXT,
    merged_at TEXT,
    tracking_until TEXT,
    baseline TEXT NOT NULL,
    tracking TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
CREATE INDEX proposals_candidate_key ON proposals (candidate_key);
CREATE INDEX proposals_status ON proposals (status);

CREATE TABLE suggestions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    evidence TEXT NOT NULL,
    target_path TEXT,
    diff TEXT,
    base_hash TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
) STRICT;
CREATE INDEX suggestions_status ON suggestions (status);
CREATE INDEX suggestions_kind ON suggestions (kind);

CREATE TABLE metric_snapshots (
    week TEXT NOT NULL,
    metric TEXT NOT NULL,
    dimension TEXT NOT NULL,
    value REAL,
    numerator REAL,
    denominator REAL,
    sample_size INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (week, metric, dimension)
) STRICT;

CREATE TABLE improve_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    reason TEXT,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE deployments (
    "commit" TEXT PRIMARY KEY,
    workflow_run_id TEXT,
    status TEXT NOT NULL,
    url TEXT,
    deployed_at TEXT,
    detected_at TEXT NOT NULL
) STRICT;
CREATE INDEX deployments_detected ON deployments (detected_at);

CREATE TABLE pulls (
    issue_id TEXT PRIMARY KEY,
    number INTEGER NOT NULL,
    url TEXT NOT NULL,
    branch TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    mergeable TEXT,
    merge_commit TEXT,
    merged_at TEXT,
    closed_at TEXT,
    close_note TEXT,
    reviews TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_reminded_at TEXT,
    master_at TEXT
) STRICT;
CREATE INDEX pulls_state ON pulls (state);

CREATE TABLE regressions (
    issue_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    requires TEXT NOT NULL,
    path TEXT NOT NULL,
    hash TEXT NOT NULL,
    base_commit TEXT,
    base_result TEXT,
    last_result TEXT NOT NULL,
    last_run_id TEXT,
    last_run_at TEXT,
    last_release TEXT,
    PRIMARY KEY (issue_id, check_id)
) STRICT;

CREATE TABLE schedule_state (
    task TEXT PRIMARY KEY,
    last_started_at TEXT,
    last_ended_at TEXT,
    last_status TEXT,
    last_run_id TEXT,
    missed_count INTEGER NOT NULL
) STRICT;

CREATE TABLE locks (
    subject_id TEXT PRIMARY KEY,
    holder_pid INTEGER NOT NULL,
    holder_host TEXT NOT NULL,
    run_id TEXT,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
) STRICT;

CREATE TABLE idempotency_keys (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE TABLE pending_operations (
    id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    executor TEXT NOT NULL,
    commands TEXT NOT NULL,
    description TEXT NOT NULL,
    impact TEXT NOT NULL,
    reversible INTEGER NOT NULL,
    preconditions TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    confirmations_required INTEGER NOT NULL,
    confirmations_given INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    executed_at TEXT,
    result TEXT
) STRICT;
CREATE INDEX pending_operations_subject ON pending_operations (subject_id);
CREATE INDEX pending_operations_status ON pending_operations (status);

CREATE TABLE agent_sessions (
    stage TEXT NOT NULL,
    role TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    tool TEXT NOT NULL,
    session_id TEXT,
    workdir TEXT NOT NULL,
    status TEXT NOT NULL,
    ended_at TEXT,
    PRIMARY KEY (stage, role, subject_id, started_at)
) STRICT;

CREATE TABLE server_log_cursors (
    file_name TEXT PRIMARY KEY,
    head_hash TEXT NOT NULL,
    "offset" INTEGER NOT NULL,
    last_local_time TEXT,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE incidental_sources (
    source_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    read_at TEXT NOT NULL,
    signal_count INTEGER NOT NULL
) STRICT;

CREATE TABLE knowledge_meta (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL,
    related TEXT NOT NULL,
    superseded_by TEXT,
    updated TEXT NOT NULL,
    review_by TEXT,
    path TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL,
    file_mtime REAL NOT NULL,
    file_size INTEGER NOT NULL,
    hits INTEGER NOT NULL,
    last_hit_at TEXT,
    indexed_at TEXT NOT NULL
) STRICT;
CREATE INDEX knowledge_meta_type ON knowledge_meta (type);
CREATE INDEX knowledge_meta_status ON knowledge_meta (status);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    id UNINDEXED, title, summary, tags, body,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE budget_usage (
    stage TEXT NOT NULL,
    date TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stage, date)
) STRICT;
