#!/usr/bin/env bash
# Full stack check used by CI: builds every image, runs both demo agents through the proxy
# against the mock upstream, waits until every evaluation finishes, then checks the numbers
# and exercises the review workflow through the review CLI. Runs as its own compose project on
# other ports, so it never touches your local stack.
set -euo pipefail
cd "$(dirname "$0")/.."

export PROXY_PORT=18000 DASHBOARD_PORT=18501 POSTGRES_PORT=15432
compose() { docker compose -p agenteval-e2e -f docker-compose.yml -f docker-compose.offline.yml "$@"; }
psql_value() { compose exec -T postgres psql -U agenteval -d agenteval -tAc "$1"; }
expect() { [ "$2" = "$3" ] || { echo "FAIL $1: expected '$3', got '$2'"; exit 1; }; echo "ok   $1 = $2"; }

cleanup() {
  compose logs --no-color > e2e-logs.txt 2>&1 || true
  compose down -v --remove-orphans > /dev/null 2>&1 || true
}
trap cleanup EXIT

compose up --build -d
for _ in $(seq 1 90); do
  curl -fs "http://127.0.0.1:${PROXY_PORT}/health/ready" > /dev/null && break
  sleep 1
done
curl -fs "http://127.0.0.1:${PROXY_PORT}/health/ready" > /dev/null

wait_idle() {
  for _ in $(seq 1 90); do
    pending=$(psql_value "SELECT (SELECT count(*) FROM traces WHERE eval_status IN ('pending','running'))
                               + (SELECT count(*) FROM trace_evaluations WHERE status IN ('pending','running'))
                               + (SELECT count(*) FROM traces t WHERE t.grounded_eval AND NOT EXISTS
                                   (SELECT 1 FROM trace_evaluations e WHERE e.trace_id = t.id))")
    [ "${pending}" = "0" ] && return 0
    sleep 1
  done
  echo "evaluations did not finish"; exit 1
}

echo "--- support triage demo"
compose --profile demo run --rm demo
wait_idle
expect "traces|errors|streamed|scored|skipped|failed|pending" \
  "$(psql_value "SELECT traces, errors, streamed, eval_scored, eval_skipped, eval_failed, eval_pending FROM trace_summary")" \
  "11|1|3|9|2|0|0"
expect "response_model recorded" \
  "$(psql_value "SELECT count(*) FROM traces WHERE status_code = 200 AND response_model = 'gpt-4o-mini-2024-07-18'")" "10"

echo "--- synthetic denial investigation demo"
compose --profile rcm run --rm rcm-demo
wait_idle
expect "grounded verdicts by case" \
  "$(psql_value "SELECT string_agg(t.case_ref || '=' || e.verdict, ',' ORDER BY t.case_ref)
                 FROM trace_evaluations e JOIN traces t ON t.id = e.trace_id WHERE e.status = 'completed'")" \
  "CLM-24-10031=supported,CLM-24-10047=unsupported,CLM-24-10052=insufficient_evidence"
expect "tool call turns skipped" "$(psql_value "SELECT count(*) FROM trace_evaluations WHERE status = 'skipped'")" "3"
expect "pending reviews" "$(psql_value "SELECT count(*) FROM review_items WHERE status = 'pending'")" "2"
expect "contact details redacted" \
  "$(psql_value "SELECT count(*) FROM traces WHERE messages::text LIKE '%example.test%'")" "0"
expect "worker heartbeat" "$(psql_value "SELECT count(*) FROM worker_heartbeats WHERE last_seen_at > now() - interval '1 minute'")" "1"

echo "--- review workflow"
review_id=$(psql_value "SELECT r.id FROM review_items r WHERE r.case_ref = 'CLM-24-10047'")
compose exec -T dashboard python review_cli.py decide "${review_id}" rejected --actor "E2E Reviewer" \
  --note "Policy version 4 (180 days) is retired; no 430.00 amount in evidence."
if compose exec -T dashboard python review_cli.py decide "${review_id}" rejected --actor "E2E Reviewer" --note x; then
  echo "FAIL duplicate decision was accepted"; exit 1
fi
compose exec -T dashboard python review_cli.py decide "${review_id}" resolved --actor "E2E Lead"
expect "audit trail" "$(psql_value "SELECT string_agg(to_status, '>' ORDER BY id) FROM review_events WHERE review_id = ${review_id}")" \
  "pending>rejected>resolved"

echo "--- service health"
curl -fs "http://127.0.0.1:${DASHBOARD_PORT}/_stcore/health" > /dev/null
expect "proxy readiness" "$(curl -fs "http://127.0.0.1:${PROXY_PORT}/health/ready" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')" "ready"
curl -fs "http://127.0.0.1:${PROXY_PORT}/metrics" | grep -q '^agenteval_traces_written_total 17$' \
  || { echo "FAIL metrics: written_total is not 17"; curl -fs "http://127.0.0.1:${PROXY_PORT}/metrics"; exit 1; }
echo "ok   metrics agenteval_traces_written_total = 17"
echo "End to end check passed"
