#!/usr/bin/env bash
# Stops the real Postgres container while traffic flows, restarts it, and checks that every
# trace arrives exactly once. Also restarts the proxy while traces are still spooled.
# Runs as its own compose project on other ports and removes its volumes at the end.
set -euo pipefail
cd "$(dirname "$0")/.."

export PROXY_PORT=28000 DASHBOARD_PORT=28501 POSTGRES_PORT=25432
compose() { docker compose -p agenteval-drill -f docker-compose.yml -f docker-compose.offline.yml "$@"; }
psql_value() { compose exec -T postgres psql -U agenteval -d agenteval -tAc "$1"; }
ready() { curl -s "http://127.0.0.1:${PROXY_PORT}/health/ready"; }
send() {
  for i in $(seq 1 "$1"); do
    curl -fs -o /dev/null "http://127.0.0.1:${PROXY_PORT}/v1/chat/completions" -H 'content-type: application/json' \
      -d "{\"model\": \"gpt-4o-mini\", \"messages\": [{\"role\": \"user\", \"content\": \"$2 ${i}\"}]}"
  done
}
cleanup() { compose down -v --remove-orphans > /dev/null 2>&1 || true; }
trap cleanup EXIT

compose up --build -d
until curl -fs "http://127.0.0.1:${PROXY_PORT}/health/ready" > /dev/null; do sleep 1; done

send 5 "before outage"
sleep 2
echo "traces before outage: $(psql_value 'SELECT count(*) FROM traces')"

echo "--- stopping Postgres"
compose stop postgres
send 20 "during outage"
sleep 3
echo "proxy readiness during outage: $(ready)"

echo "--- restarting the proxy while 20 traces are spooled (graceful stop cannot drain)"
compose restart proxy
until curl -s "http://127.0.0.1:${PROXY_PORT}/health/live" > /dev/null; do sleep 1; done
echo "proxy readiness after restart: $(ready)"

echo "--- starting Postgres"
compose start postgres
for _ in $(seq 1 60); do
  depth=$(ready | python3 -c 'import json,sys; print(json.load(sys.stdin)["trace_store"]["spool_depth"])' 2>/dev/null || echo "?")
  [ "${depth}" = "0" ] && break
  sleep 1
done
total=$(psql_value "SELECT count(*) FROM traces")
distinct=$(psql_value "SELECT count(DISTINCT trace_id) FROM traces")
delay=$(psql_value "SELECT round(max(extract(epoch FROM ingested_at - created_at))) FROM traces")
echo "traces after recovery: ${total} (distinct trace ids ${distinct}), max ingestion delay ${delay}s"
echo "proxy readiness after recovery: $(ready)"
[ "${total}" = "25" ] && [ "${distinct}" = "25" ] || { echo "FAIL expected 25 traces exactly once"; exit 1; }
echo "Outage drill passed"
