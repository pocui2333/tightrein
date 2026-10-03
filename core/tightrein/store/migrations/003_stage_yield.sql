-- 环节效益(design 14.2，architecture/01 4.2)：每次实际调用过工具的 LLM 调用一行，由调用执行器的模块登记，
-- outcome 由 learn 在结果确定后回填。RunnerResult 不带 span 编号，以自增 id 为主键；runner_status 为执行器的结果状态，
-- 没有返回结果的调用在回填时直接判为无有效产出。

CREATE TABLE stage_yield (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    role TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    runner_status TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    outcome TEXT NOT NULL,
    outcome_reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
) STRICT;
CREATE INDEX stage_yield_stage_role ON stage_yield (stage, role, created_at);
CREATE INDEX stage_yield_outcome ON stage_yield (outcome);
