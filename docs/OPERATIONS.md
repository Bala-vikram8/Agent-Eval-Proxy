# Operator runbook

This runbook covers the single-organization local system and the proposed private staging topology. It is not evidence that a cloud deployment or disaster recovery exercise has occurred.

## Start, inspect and stop locally

Use the offline Compose pair for synthetic traffic. `up --build -d --wait` applies the serialized schema migration and role bootstrap before application startup. Use `docker compose ... logs --tail 100` for the specific service and the authenticated proxy `/metrics` endpoint for admission/ingestion counters. Liveness `/health` is intentionally distinct from readiness `/ready`; an open port alone is not a useful functional test.

`docker compose ... down` stops services and retains database/spool volumes. Do not use `down -v` on a development or production dataset you intend to retain. The isolated E2E script generates its own project and removes only its temporary volumes. It pins synthetic credentials and random host ports independently of the developer's `.env`.

The migration role owns schema changes. `agenteval_ingest` can insert traces, select only their event identity and read erasure tombstones; `agenteval_eval` can read evaluation input and update only lifecycle/relevance/cohort/usage columns, plus write history/heartbeats. It cannot rewrite trace content or event identity. `agenteval_dashboard` reads approved views/tables only; `agenteval_monitor` reads evaluation data and writes alerts/its heartbeat. Application roles are not superusers. Alert acknowledgement requires a separately configured operator credential and explicit UI capability; do not give the normal dashboard write access.

The Postgres health check requires an authenticated TCP query to the selected database, not the temporary socket-only server used by the official image during initialization. This prevents migrations from racing initial database creation. Proxy access logs are disabled to avoid putting unredacted query strings into container logs; use safe structured counters/events, and never send credentials or personal data in URL query parameters. Provider/edge logging needs its own reviewed privacy policy.

## Triage by symptom

| Symptom | Inspect | Action |
|---|---|---|
| 401/authentication startup error | Correct proxy credential vs provider credential; dashboard secret presence | Deliver/rotate credentials through the configured secret manager. Do not paste secrets into logs or chat. Insecure-local mode is only for loopback synthetic development. |
| 400/413/415 | Request shape, size, encoding and safe agent label | Correct the client. Do not disable limits to accept malformed data. |
| 429 or overload 503 | Per-process quota/active count and edge limits | Reduce offered load; measure capacity before increasing bounds. Per-process token buckets are not distributed quotas. |
| `trace_unavailable` 503 | Queue availability, full spool, dead letters, rejected counters | Provider work may already have completed. Avoid automatic blind retries that incur duplicate charges. Restore queue/consumer capacity and reconcile by trace event ID. |
| HTTP stream ends early | Failure category, reconstructed `stream_completed`, trace acceptance counters | Distinguish client disconnect, upstream failure, capture limit and malformed stream. Already-sent stream headers cannot report a later queue failure. |
| Traces pending in queue | Consumer health, DB role/TLS/schema, retry category and oldest age | Restore dependency, let leased work recover and inspect retry/DLQ counts. Do not acknowledge/delete until the database insert commits. |
| Evaluation pending/processing | Worker heartbeat, lease expiry, next-attempt time, judge availability/budget | Wait for backoff/lease or correct configuration. A crashed attempt counts toward the cap. Do not manually clear active lease tokens. |
| Evaluation skipped/insufficient evidence | Metadata-only capture, missing trusted catalog/evidence/schema, sampling | Skipped does not mean passed or failed. Enable approved redacted capture or operator evidence only when privacy permits. |
| Evaluation failed | Versioned history, safe error category, attempt count | Correct cause and use an audited, parameterized requeue procedure after reviewing budget; do not erase history. |
| Dashboard database error | Server-side safe category, read-only role, schema and network/TLS | Restore service or grants. User-facing responses intentionally omit database addresses/payloads. |
| Quality alert | Matched cohort/evaluator version, independent sample counts, effect/confidence, request mix | Investigate retrieval/prompt/tool/model/config changes. Score deterioration is not proof of model drift. Small or unmatched samples must not generate a statistical claim. |

## Retention and deletion

Use the retention CLI with a dedicated administrative connection, never the dashboard or ingestion role. Review its exact options with `python eval/retention.py --help`; use dry-run before an explicit apply. Batch limits bound lock time. This implementation does not run a destructive retention command on production data during setup.

Privacy lifetime is more than the `traces` table: evaluation history, local spool and dead letters, SQS/DLQ, backups and exported reports all need approved lifetimes. SQL erasure tombstones retain event IDs so redelivery of the same event cannot recreate erased rows; ingestion and deletion serialize on the event key. Keep tombstones for at least as long as any possible replay source, including restored backups. Broker payload removal and backup erasure remain separate procedures. Review counts of intentionally purged/expired events; do not label them unexplained capture loss.

Do not replay a dead letter until its cause and privacy eligibility are checked. Keep the stable event ID on replay so the sink can deduplicate it. Never substitute an upstream provider request ID for the proxy event identity.

## Outages and shutdown

Database outage: the bounded retained spool/SQS continues to accept until its admission policy rejects. The consumer retries with backoff and sends exhausted/invalid work to dead-letter handling. SQS backlog bounds rely on approximate service attributes; they are not an exact distributed capacity reservation. SQLite counts/bytes are strict queue bounds on one filesystem.

Consumer crash: visibility/lease expiry makes unacknowledged events available again. A crash after commit but before ACK can cause duplicate delivery; the unique event key prevents duplicate database rows. Evaluation claims reserve attempts before remote calls; expired leases recover and completion is fenced against stale workers.

Queue outage/full queue: nonstreamed trace rejection is a visible error; streaming capture failure is exposed through counters because headers may already have been sent. Model execution and trace capture are not an atomic transaction. Neither SQLite nor SQS can recover bytes that never reached the proxy.

Graceful SIGTERM stops new consumer work and performs a bounded drain; unfinished durable events remain for a replacement worker. Container stop time must exceed configured shutdown/query timeouts. Monitor alerting should include missing worker heartbeats, queue age, DLQ depth and missing trace/capture counters; a quiet chart is not proof that all events arrived.

## Backup, incident and spending controls

Test database restore into an isolated copy before promising an RPO/RTO. Record the actual dataset, counts, time to usable service, queue backlog and policy reapplication. Keep previous application digests and migration metadata. Snapshot settings alone are not a recovery exercise.

During a suspected credential incident: restrict ingress, revoke the provider credential, rotate deployment-specific keys, preserve minimized metadata evidence and review outbound usage. Redaction does not guarantee that historical data is anonymous.

Set project budget and provider-spend alerts. Budget notifications are delayed and do not stop billing. The synthetic examples have zero provider charge but still consume compute. Scale or stop staging only through an approved procedure that preserves necessary volumes/backups and does not delete production data.

For an actual local outage/crash exercise, run `python scripts/recovery_exercise.py` with a loopback `TEST_DATABASE_URL`. It creates a private spool and temporary database, demonstrates failed database connections and durable retry, kills only its own ingestion worker, restarts with the working database, verifies recovery and duplicate suppression, then cleans up. Its JSON records the measured recovery time and configured lease. This does not exercise RDS backup restoration or real SQS failure.

Cloud SQS consumers use `TRACE_SQS_WAIT_SECONDS=20` by default to avoid continuous empty receives. Control calls use a separate short timeout. Shutdown does not wait indefinitely for an empty long poll; unacknowledged deliveries remain recoverable by visibility timeout. Queue depths/admission observations remain approximate.
