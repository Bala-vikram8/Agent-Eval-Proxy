#!/usr/bin/env bash
# Full stack check used by CI: builds every image, runs the demo agent through the proxy
# against the mock upstream, waits until every trace is judged, then checks the numbers.
# Runs as its own compose project on other ports, so it never touches your local stack.
set -euo pipefail
cd "$(dirname "$0")/.."

export PROXY_PORT=18000 DASHBOARD_PORT=18501 POSTGRES_PORT=15432
compose() { docker compose -p agenteval-e2e -f docker-compose.yml -f docker-compose.offline.yml "$@"; }
psql_value() { compose exec -T postgres psql -U agenteval -d agenteval -tAc "$1"; }

cleanup() {
  compose logs --no-color > e2e-logs.txt 2>&1 || true
  compose down -v --remove-orphans > /dev/null 2>&1 || true
}
trap cleanup EXIT

compose up --build -d
for _ in $(seq 1 90); do
  curl -fs "http://127.0.0.1:${PROXY_PORT}/health" > /dev/null && break
  sleep 1
done
curl -fs "http://127.0.0.1:${PROXY_PORT}/health" > /dev/null

compose --profile demo run --rm demo

echo "Waiting for the eval worker"
for _ in $(seq 1 60); do
  [ "$(psql_value "SELECT count(*) FROM traces WHERE eval_status = 'pending'")" = "0" ] && break
  sleep 1
done

summary=$(psql_value "SELECT traces, errors, streamed, eval_scored, eval_skipped, eval_failed, eval_pending FROM trace_summary")
echo "traces|errors|streamed|scored|skipped|failed|pending = ${summary}"
[ "${summary}" = "11|1|3|9|2|0|0" ] || { echo "unexpected summary"; exit 1; }

curl -fs "http://127.0.0.1:${DASHBOARD_PORT}/_stcore/health" > /dev/null
echo "End to end check passed"
