-- Schema of the second version (commit 12222dc), kept to test the upgrade path.
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
    cost_usd NUMERIC(10,6),
    tool_calls JSONB,
    status_code INTEGER,
    faithfulness_score NUMERIC(3,2),
    faithfulness_reason TEXT,
    tool_correct BOOLEAN,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);
CREATE INDEX IF NOT EXISTS idx_traces_score ON traces(faithfulness_score);
