-- Local development role bootstrap, applied by the migration owner after init.sql.
-- Password values come from the process environment, never committed production secrets.
-- Cloud provisioners should create equivalent roles using secure secret delivery.
\getenv ingest_password INGEST_DB_PASSWORD
\getenv eval_password EVAL_DB_PASSWORD
\getenv dashboard_password DASHBOARD_DB_PASSWORD
\getenv monitor_password MONITOR_DB_PASSWORD
SELECT 'CREATE ROLE agenteval_ingest LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agenteval_ingest') \gexec
SELECT 'CREATE ROLE agenteval_eval LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agenteval_eval') \gexec
SELECT 'CREATE ROLE agenteval_dashboard LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agenteval_dashboard') \gexec
SELECT 'CREATE ROLE agenteval_monitor LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agenteval_monitor') \gexec
-- Managed database administrators need not be PostgreSQL superusers. They may
-- create restricted roles but cannot change SUPERUSER/REPLICATION attributes.
-- Refuse unsafe pre-existing identities rather than attempting a privileged
-- demotion or silently inheriting unrelated access. New roles default safe.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname IN ('agenteval_ingest', 'agenteval_eval', 'agenteval_dashboard', 'agenteval_monitor')
          AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
        WHERE r.rolname IN ('agenteval_ingest', 'agenteval_eval', 'agenteval_dashboard', 'agenteval_monitor')
    ) THEN
        RAISE EXCEPTION 'Application roles must have restricted capabilities and no inherited role memberships';
    END IF;
END $$;
ALTER ROLE agenteval_ingest WITH LOGIN PASSWORD :'ingest_password';
ALTER ROLE agenteval_eval WITH LOGIN PASSWORD :'eval_password';
ALTER ROLE agenteval_dashboard WITH LOGIN PASSWORD :'dashboard_password';
ALTER ROLE agenteval_monitor WITH LOGIN PASSWORD :'monitor_password';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT CONNECT ON DATABASE %I TO agenteval_ingest, agenteval_eval, agenteval_dashboard, agenteval_monitor', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO agenteval_ingest, agenteval_eval, agenteval_dashboard, agenteval_monitor;
-- Revoke the earlier broad grants before installing the current capabilities.
-- ON CONFLICT needs visibility of its event key, not message/response content.
REVOKE SELECT ON traces FROM agenteval_ingest;
GRANT SELECT (event_id), INSERT ON traces TO agenteval_ingest;
GRANT SELECT ON trace_tombstones TO agenteval_ingest;
GRANT USAGE, SELECT ON SEQUENCE traces_id_seq TO agenteval_ingest;
REVOKE UPDATE ON traces FROM agenteval_eval;
GRANT SELECT ON traces TO agenteval_eval;
GRANT UPDATE (
    eval_status, eval_attempts, eval_next_attempt_at, eval_error, evaluated_at,
    eval_lease_token, eval_lease_until, eval_cohort,
    relevance_score, relevance_reason, judge_model,
    judge_prompt_tokens, judge_completion_tokens
) ON traces TO agenteval_eval;
GRANT SELECT ON traces_enriched TO agenteval_eval;
GRANT SELECT ON traces, traces_enriched, trace_summary, model_summary, model_prices, evaluation_runs, alerts, worker_heartbeats TO agenteval_dashboard;
-- Tables below are created by newer schema versions; safely grant only when present.
SELECT format('GRANT SELECT, INSERT ON %I TO agenteval_eval', tablename)
FROM pg_tables WHERE schemaname = 'public' AND tablename = 'evaluation_runs' \gexec
SELECT format('GRANT SELECT, INSERT, UPDATE ON %I TO agenteval_eval', tablename)
FROM pg_tables WHERE schemaname = 'public' AND tablename = 'worker_heartbeats' \gexec
GRANT USAGE, SELECT ON SEQUENCE evaluation_runs_id_seq TO agenteval_eval;
GRANT SELECT ON traces, traces_enriched, evaluation_runs, alerts, worker_heartbeats TO agenteval_monitor;
SELECT format('GRANT INSERT, UPDATE ON %I TO agenteval_monitor', tablename)
FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alerts' \gexec
GRANT USAGE, SELECT ON SEQUENCE alerts_id_seq TO agenteval_monitor;
SELECT format('GRANT SELECT, INSERT, UPDATE ON %I TO agenteval_monitor', tablename)
FROM pg_tables WHERE schemaname = 'public' AND tablename = 'worker_heartbeats' \gexec
