# Validation evidence and release boundaries

Validation dates: 2026-11-01–2026-11-02. Baseline main was `d888c450a067b17bb6436b4d896274eccbba9a02`; the audit commit precedes the implementation. Results below describe this development environment and the tested scope, not production readiness. Raw generated evidence is saved under the ignored `artifacts/` directory so local runs do not commit sensitive future test output accidentally.

## Phase outcomes

| Workstream | Implemented files / behavior | Evidence | Remaining boundary |
|---|---|---|---|
| Security/privacy | `proxy/main.py`, `privacy.py`, `security.py`, `streaming.py`, dashboard auth, container commands: fail-closed auth, credential separation, linear-time redaction, metadata default, request/stream bounds, safe logs/errors | Authentication/privacy/HTTP/deadline/malformed-stream regressions; full suite below | Redaction is not anonymization; shared application credentials are not cloud SSO or tenancy; global quotas require a gateway |
| Reliable ingestion | `proxy/queueing.py`, `store.py`, `ingest_worker.py`: durable bounded spool, SQS adapter, committed idempotent insert, retries/DLQ, recovery, safe counters and fair local access | 51 focused ingestion tests; final load and recovery evidence below | SQLite is one retained filesystem; real SQS/IAM/network behavior untested; stream capture is deferred |
| Evaluation | `eval/evaluators.py`, `eval_engine.py`, `retention.py`, `sql/init.sql`: committed leases/fencing, history/config versions, trusted catalogs, finite judge attempts, deterministic checks, retention/tombstones, pricing fixes | Golden regression/calibration helpers and real-Postgres lease/migration/deletion tests | Real judge accuracy/calibration and domain references require independent validation; exact reference agreement is not verified truth |
| Monitoring/dashboard | `dashboard/app.py`, `auth.py`, `monitoring.py`, `monitor.py`: filtered investigation, history, queue/heartbeat signals, candidate alerts and restricted acknowledgement | Synthetic deterioration/stable/insufficient-data tests; actual headless investigation/acknowledgement | Independent samples and operator request-cohort labels assumed; per-comparison confidence is not a causal drift claim |
| Delivery | Compose, Dockerfiles, restricted SQL roles, CI scans, `infra/aws` and runbooks | Real restricted-role checks, dependency scan and mock-provider infrastructure tests | AWS remains an undeployed scaffold; real cloud access, rollout and restore require approval and validation |
| Demonstration | `examples/automotive_*`, `tests/test_automotive.py`, recovery exercise | Actual synthetic requests/tool calls/evaluations/alerts/UI plus regression assertions | Fictional workflow, deterministic provider and controlled monitoring timestamps; no customer-adoption claim |

## Tests and static checks

The final combined application suite executed **261 tests, all passed, no skips**, in 57.42 seconds. This includes security/privacy, real-Postgres evaluation/migration/deletion, durable queue/SQS-adapter, 50-publisher contention, dashboard/monitoring, benchmark reporting and simulated integration regressions. The earlier 248-test run also passed; subsequent queue diagnostics added the remaining cases.

```bash
TEST_DATABASE_URL=postgresql://agenteval:agenteval@127.0.0.1:5432/agenteval \
  .venv/bin/python -m pytest --junitxml=artifacts/pytest-final.xml
```

Terraform formatting, schema validation and **five mock-provider plan tests passed**. They check private/stopped defaults, encryption/backups, real health commands/private metrics routing, immutable images and rejection of open/supernet client networks. Mock plans do not create AWS resources. The provider plugin was installed with signature/checksum verification.

Local runtime-role verification passed: ingestion can insert/deduplicate and read erasure identity but cannot read message content; evaluation can claim/complete but cannot alter messages; dashboard reads views and cannot delete. The role script ran twice under a local non-superuser database-owner/CREATEROLE capability simulation, and refused elevated pre-existing application roles. This is not an RDS runtime test.

## Load test: failure, diagnosis and correction

Before the queue fix, the comparable 50-client diagnostic run had **31 HTTP 503 trace-publication failures out of 1,000 concurrent requests**. All 1,119 durably acknowledged traces (including warm-up/sequential calls) reached Postgres. A second code-diagnostic run confirmed `SQLITE_BUSY`; it was deliberately labeled contended-environment diagnostic, not a capacity measurement.

The repair serializes short in-process SQLite operations through a bounded fair gate. It retains full filesystem synchronization, the existing two-second wait bounds, capacity limits, cross-process database locking and leases. An unfair lock was rejected after its stress regression exposed starvation. Failed artifacts remain available rather than being replaced by passing numbers.

After the repair, the matching 50-client run captured **1,150/1,150** proxy traces with zero request failures. The original larger default workload also passed:

```bash
TEST_DATABASE_URL=postgresql://agenteval:agenteval@127.0.0.1:5432/agenteval \
  .venv/bin/python bench/overhead.py
```

| Measurement | Actual default run |
|---|---:|
| Proxy traces, including warm-up | 3,050 / 3,050 |
| HTTP failures / missing acknowledged events | 0 / 0 |
| Concurrent clients / measured concurrent requests | 50 / 2,000 |
| Successful concurrent throughput | 95.77 requests/second |
| Concurrent proxy p50 / p95 | 511.80 / 626.02 ms |
| Sequential proxy p50 / p95 | 9.32 / 14.42 ms |
| Sequential direct fixture p50 / p95 | 0.84 / 1.73 ms |
| Concurrent direct fixture p50 / p95 | 114.81 / 567.90 ms |
| Post-workload database + final queue-ACK drain | 12.56 seconds |
| Queue publication / database / consumer errors | 0 / 0 / 0 |
| Child user / system CPU | 34.83 / 15.83 seconds |
| Largest child peak RSS | 112,688 KiB |

Scope: Linux x86-64, Python 3.12, four cgroup CPU cores (five visible), local deterministic upstream with a configured 100 ms delay in the concurrent phase, metadata-only capture and embedded SQLite ingestion. Memory excludes Postgres/host total. These numbers do not measure real-provider networking, paid judge throughput, redacted-content overhead, cloud capacity or a production SLA. Artifact: `artifacts/benchmark-default-fixed.json`.

## Synthetic integration

The actual automotive simulation produced **360/360 traces** across 60 baseline, 60 incident and 60 recovered tasks (each with a tool turn and answer turn). Reference means were 1.0, 0.0 and 1.0 respectively. The monitor persisted two candidate alerts, deduplicated repeat checks, produced no new deterioration alert after the fix, and the real Streamlit application rendered investigation data and recorded acknowledgement headlessly. Its five tool/config/stream regressions passed.

Answer HTTP latency in that run was median **24.64 ms**, p95 **37.72 ms**. Scores/token counts are fixture outputs; monitoring timestamps were controlled synthetic values. No paid model service or actual dealership data was used. Artifact: `artifacts/automotive/results.json`.

## Actual local crash and outage recovery

`python scripts/recovery_exercise.py` successfully exercised a retained local spool with 100 synthetic events. A real worker encountered refused database connections and recorded retries, was killed with SIGKILL (exit -9), and was restarted with the working local database. All 100 events recovered in **15.09 seconds** with a configured 15-second visibility lease. Replaying the same 100 identities after acknowledgement left exactly 100 database rows; all 200 deliveries were acknowledged, with zero pending/leased/dead events and no queue errors. Only the exercise's generated database, spool and processes were affected. Artifact: `artifacts/recovery/results.json`.

This measures local ingestion restart recovery, not database backup restoration, RDS recovery, live SQS failover or an approved production recovery objective.

## Security scanning and container validation

The initial installed-environment PyPI audit identified advisories in bundled pip 25.0.1. Requirement sets were updated to patched pip 26.2; the subsequent scan of 103 installed distributions reported **no known vulnerabilities**. This is point-in-time evidence, not proof that dependencies have no unknown vulnerabilities. Raw before/after reports and package inventory are under `artifacts/security/`.

The isolated Docker end-to-end run passed after correcting a Postgres temporary-server readiness race: its trace summary was **11 traces, 1 intentional provider error, 3 streams, 9 relevance-scored, 2 skipped, 0 failed, 0 pending**. It used a fresh isolated database/spool and checked restricted role grants and worker/service readiness. The final repeat also passed after removing runtime build tools and switching to the minimized, non-root, client-only migration image. The same expected trace summary and runtime-role checks passed.

Actual Trivy scans identified release-blocking high/critical findings, including vulnerable vendored build-tool components and operating-system packages. Unneeded pip/setuptools/wheel were removed from Python runtime images; the migration artifact also drops its unused privileged helper/server packages through supported package removal. Final scans reported 46 HIGH / 0 CRITICAL per Python image, 56 HIGH / 0 CRITICAL for the migration client, and 88 HIGH / 2 CRITICAL for the unchanged local Postgres server. The exact findings and unresolved release blockers are recorded in [Container security](CONTAINER_SECURITY.md). The CI scan gate remains blocking: no advisory suppression, ignore-unfixed option or TLS bypass was added. Passing application tests does not override this failed security gate.

## Production gate

No paid cloud resources, public endpoints or production deployments were created. No live AWS SQS delivery/redrive, IAM/Secrets/KMS behavior, private routing, certificate/RDS trust, cloud volume permissions, protected deployment automation, real-provider calibration, backup restoration or production capacity was validated. A real account/platform, approved scope/budget, operator ownership and explicit deployment approval remain prerequisites. Local recovery is not a cloud disaster-recovery exercise. Regulatory compliance and perfect evaluations are not claimed.
