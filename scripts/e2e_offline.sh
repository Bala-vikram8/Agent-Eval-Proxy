#!/usr/bin/env bash
# Synthetic isolated full-stack check. Cleans only its own project and volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
project="agenteval-e2e-$$-${RANDOM}"
export PROXY_PORT=0 DASHBOARD_PORT=0 POSTGRES_PORT=0
export POSTGRES_USER=agenteval POSTGRES_PASSWORD=agenteval POSTGRES_DB=agenteval
export INGEST_DB_PASSWORD=ingest_dev_only EVAL_DB_PASSWORD=eval_dev_only
export DASHBOARD_DB_PASSWORD=dashboard_dev_only MONITOR_DB_PASSWORD=monitor_dev_only
export PROXY_API_KEY= OPENAI_API_KEY= UPSTREAM_API_KEY=offline
export JUDGE_API_KEY=offline DASHBOARD_PASSWORD=
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost"
export no_proxy="${no_proxy:-},127.0.0.1,localhost"
compose() { docker compose --env-file /dev/null -p "$project" -f docker-compose.yml -f docker-compose.offline.yml "$@"; }
psql_value() { compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"; }
cleanup() {
  compose logs --no-color > "e2e-logs-${project}.txt" 2>&1 || true
  cp "e2e-logs-${project}.txt" e2e-logs.txt || true
  compose down -v --remove-orphans > /dev/null 2>&1 || true
}
trap cleanup EXIT

compose up --build -d --wait --wait-timeout 180
proxy_address=$(compose port proxy 8000)
dashboard_address=$(compose port dashboard 8501)
curl -fsS --max-time 5 "http://${proxy_address}/ready" > /dev/null
compose --profile demo run --rm demo

echo "Waiting for ingestion and evaluation"
for _ in $(seq 1 90); do
  [ "$(psql_value "SELECT count(*) FROM traces")" = "11" ] && \
    [ "$(psql_value "SELECT count(*) FROM traces WHERE eval_status IN ('pending','processing')")" = "0" ] && break
  sleep 1
done
summary=$(psql_value "SELECT traces, errors, streamed, eval_scored, eval_skipped, eval_failed, eval_pending FROM trace_summary")
echo "traces|errors|streamed|scored|skipped|failed|pending = ${summary}"
[ "${summary}" = "11|1|3|9|2|0|0" ] || { echo "unexpected summary"; exit 1; }
# Application roles must never inherit bootstrap superuser privileges.
[ "$(psql_value "SELECT count(*) FROM pg_roles WHERE rolname IN ('agenteval_ingest','agenteval_eval','agenteval_dashboard','agenteval_monitor') AND rolsuper")" = "0" ]
[ "$(psql_value "SELECT has_column_privilege('agenteval_ingest','traces','event_id','SELECT') AND NOT has_column_privilege('agenteval_ingest','traces','messages','SELECT')")" = "t" ]
[ "$(psql_value "SELECT has_column_privilege('agenteval_eval','traces','eval_status','UPDATE') AND NOT has_column_privilege('agenteval_eval','traces','messages','UPDATE') AND NOT has_column_privilege('agenteval_eval','traces','event_id','UPDATE')")" = "t" ]
[ "$(psql_value "SELECT NOT has_table_privilege('agenteval_dashboard','traces','INSERT') AND NOT has_table_privilege('agenteval_dashboard','traces','UPDATE')")" = "t" ]
curl -fsS --max-time 5 "http://${dashboard_address}/_stcore/health" > /dev/null
echo "End to end check passed"
