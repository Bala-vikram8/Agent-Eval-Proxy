-- Schema of the first version (commit d52bcdb), kept to test the upgrade path.
CREATE TABLE IF NOT EXISTS traces (
    id SERIAL PRIMARY KEY,
    request_id TEXT,
    model TEXT,
    messages JSONB,
    response JSONB,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    latency_ms INTEGER,
    cost_usd DECIMAL(10,6),
    tool_calls JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traces_created_at ON traces(created_at);
CREATE INDEX IF NOT EXISTS idx_traces_model ON traces(model);

CREATE TABLE IF NOT EXISTS evaluations (
    id SERIAL PRIMARY KEY,
    trace_id INTEGER REFERENCES traces(id),
    faithfulness_score DECIMAL(3,2),
    tool_correctness BOOLEAN,
    judge_reason TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
