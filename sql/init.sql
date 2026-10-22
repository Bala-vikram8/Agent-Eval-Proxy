-- AgentEval schema.
-- Every statement is idempotent, so the migrate service runs this file on every start.
-- Databases created by earlier versions of the project are upgraded in place.

SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS traces (
    id                      SERIAL PRIMARY KEY,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id              TEXT,
    model                   TEXT,
    messages                JSONB,
    response                JSONB,
    status_code             INTEGER,
    streamed                BOOLEAN NOT NULL DEFAULT false,
    prompt_tokens           INTEGER,
    completion_tokens       INTEGER,
    total_tokens            INTEGER,
    latency_ms              INTEGER,
    ttft_ms                 INTEGER,
    tool_calls              JSONB,
    eval_status             TEXT NOT NULL DEFAULT 'pending',
    eval_attempts           INTEGER NOT NULL DEFAULT 0,
    eval_next_attempt_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    eval_error              TEXT,
    evaluated_at            TIMESTAMPTZ,
    relevance_score         NUMERIC(3,2),
    relevance_reason        TEXT,
    judge_model             TEXT,
    judge_prompt_tokens     INTEGER,
    judge_completion_tokens INTEGER
);

-- Upgrade path from earlier versions ------------------------------------------------

-- The first versions stored an "answer relevance" score under a faithfulness name.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'traces' AND column_name = 'faithfulness_score')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'traces' AND column_name = 'relevance_score') THEN
        ALTER TABLE traces RENAME COLUMN faithfulness_score TO relevance_score;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'traces' AND column_name = 'faithfulness_reason')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'traces' AND column_name = 'relevance_reason') THEN
        ALTER TABLE traces RENAME COLUMN faithfulness_reason TO relevance_reason;
    END IF;
    -- created_at used to be a timestamp without time zone, written in UTC.
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'traces' AND column_name = 'created_at'
                 AND data_type = 'timestamp without time zone') THEN
        ALTER TABLE traces ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';
    END IF;
END $$;

ALTER TABLE traces ADD COLUMN IF NOT EXISTS status_code             INTEGER;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS streamed                BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS ttft_ms                 INTEGER;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS eval_status             TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE traces ADD COLUMN IF NOT EXISTS eval_attempts           INTEGER NOT NULL DEFAULT 0;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS eval_next_attempt_at    TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE traces ADD COLUMN IF NOT EXISTS eval_error              TEXT;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS evaluated_at            TIMESTAMPTZ;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS relevance_score         NUMERIC(3,2);
ALTER TABLE traces ADD COLUMN IF NOT EXISTS relevance_reason        TEXT;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS judge_model             TEXT;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS judge_prompt_tokens     INTEGER;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS judge_completion_tokens INTEGER;
ALTER TABLE traces ALTER COLUMN created_at SET DEFAULT now();

-- Cost is derived from token counts and model_prices (see traces_enriched), so a price
-- fix corrects history. The stored column priced every model at one fixed rate.
ALTER TABLE traces DROP COLUMN IF EXISTS cost_usd;
-- Only ever checked that a tool call was well formed, which the API already guarantees.
ALTER TABLE traces DROP COLUMN IF EXISTS tool_correct;

-- Rows from the very first version have no status code. A body with choices was a success.
UPDATE traces SET status_code = 200
WHERE status_code IS NULL AND jsonb_typeof(response) = 'object' AND response ? 'choices';
-- Rows that already carry a score should not be judged again.
UPDATE traces SET eval_status = 'scored', evaluated_at = coalesce(evaluated_at, created_at)
WHERE eval_status = 'pending' AND relevance_score IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'traces_eval_status_check') THEN
        ALTER TABLE traces ADD CONSTRAINT traces_eval_status_check
            CHECK (eval_status IN ('pending', 'scored', 'skipped', 'failed'));
    END IF;
END $$;

DROP INDEX IF EXISTS idx_traces_created_at;
DROP INDEX IF EXISTS idx_traces_created;
DROP INDEX IF EXISTS idx_traces_model;
DROP INDEX IF EXISTS idx_traces_score;
CREATE INDEX IF NOT EXISTS traces_created_at_idx ON traces (created_at);
-- The eval worker claims pending rows in id order; this keeps that lookup small.
CREATE INDEX IF NOT EXISTS traces_eval_pending_idx ON traces (id) WHERE eval_status = 'pending';

-- Prices ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS model_prices (
    model                TEXT PRIMARY KEY,
    input_usd_per_mtok   NUMERIC(10,4) NOT NULL,
    output_usd_per_mtok  NUMERIC(10,4) NOT NULL,
    source               TEXT,
    checked_on           DATE
);

-- Seed rows never overwrite a price you have edited. Add your own models the same way.
INSERT INTO model_prices (model, input_usd_per_mtok, output_usd_per_mtok, source, checked_on)
VALUES ('gpt-4o-mini', 0.15, 0.60, 'https://anotherwrapper.com/llm-pricing/gpt-4o-mini-2024-07-18', DATE '2026-10-22')
ON CONFLICT (model) DO NOTHING;

-- Views used by the dashboard ----------------------------------------------------------

DROP VIEW IF EXISTS model_summary;
DROP VIEW IF EXISTS trace_summary;
DROP VIEW IF EXISTS traces_enriched;

-- Model ids like gpt-4o-mini-2024-07-18 are priced as gpt-4o-mini.
CREATE VIEW traces_enriched AS
SELECT
    t.*,
    (t.prompt_tokens * p.input_usd_per_mtok + t.completion_tokens * p.output_usd_per_mtok) / 1000000.0
        AS cost_usd,
    (t.judge_prompt_tokens * jp.input_usd_per_mtok + t.judge_completion_tokens * jp.output_usd_per_mtok) / 1000000.0
        AS judge_cost_usd,
    (t.status_code = 200 AND t.prompt_tokens IS NOT NULL AND p.model IS NULL) AS unpriced
FROM traces t
LEFT JOIN model_prices p
       ON p.model = regexp_replace(t.model, '-\d{4}-\d{2}-\d{2}$', '')
LEFT JOIN model_prices jp
       ON jp.model = regexp_replace(t.judge_model, '-\d{4}-\d{2}-\d{2}$', '');

CREATE VIEW trace_summary AS
SELECT
    count(*)                                                        AS traces,
    count(*) FILTER (WHERE status_code IS DISTINCT FROM 200)         AS errors,
    round(100.0 * count(*) FILTER (WHERE status_code IS DISTINCT FROM 200)
          / nullif(count(*), 0), 1)                                 AS error_rate_pct,
    round((percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms)
           FILTER (WHERE status_code = 200))::numeric)               AS p50_latency_ms,
    round((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
           FILTER (WHERE status_code = 200))::numeric)               AS p95_latency_ms,
    round((percentile_cont(0.5)  WITHIN GROUP (ORDER BY ttft_ms)
           FILTER (WHERE streamed AND status_code = 200))::numeric)  AS p50_ttft_ms,
    count(*) FILTER (WHERE streamed)                                AS streamed,
    coalesce(sum(cost_usd), 0)                                      AS model_cost_usd,
    coalesce(sum(judge_cost_usd), 0)                                AS judge_cost_usd,
    count(*) FILTER (WHERE unpriced)                                AS unpriced_traces,
    avg(relevance_score)                                            AS avg_relevance,
    count(*) FILTER (WHERE eval_status = 'scored')                  AS eval_scored,
    count(*) FILTER (WHERE eval_status = 'skipped')                 AS eval_skipped,
    count(*) FILTER (WHERE eval_status = 'failed')                  AS eval_failed,
    count(*) FILTER (WHERE eval_status = 'pending')                 AS eval_pending
FROM traces_enriched;

CREATE VIEW model_summary AS
SELECT
    model,
    count(*)                                                        AS traces,
    count(*) FILTER (WHERE status_code IS DISTINCT FROM 200)         AS errors,
    round((percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
           FILTER (WHERE status_code = 200))::numeric)               AS p50_latency_ms,
    sum(prompt_tokens)                                              AS prompt_tokens,
    sum(completion_tokens)                                          AS completion_tokens,
    round(sum(cost_usd), 6)                                         AS cost_usd,
    bool_or(unpriced)                                               AS unpriced,
    round(avg(relevance_score), 2)                                  AS avg_relevance,
    count(relevance_score)                                          AS scored
FROM traces_enriched
GROUP BY model
ORDER BY traces DESC;
