# Agent Eval Proxy

An OpenAI-compatible gateway for tracing agent requests, asynchronous evaluations, and operational investigation. This project is being hardened from a working local demo into a deployable service. **It is not yet validated for production, sensitive customer data, or multiple independent customers.**

The [container security gate](docs/CONTAINER_SECURITY.md) remains blocked by unresolved upstream operating-system advisories. Local functional success does not clear that release gate.

The LLM judge measures **answer relevance**, not factual truth. Deterministic checks can compare an answer with an operator-supplied reference, validate tool arguments and structured output, and flag configured operational limits. Matching a reference does not prove the reference is correct. Lexical evidence coverage is an indicator, not groundedness or entailment verification.

## Architecture

```mermaid
flowchart LR
    A[Agent / OpenAI client] --> P[Authenticated FastAPI proxy]
    P --> U[Configured model provider]
    P --> M[Metadata minimization / optional redaction]
    M --> Q[Bounded durable local spool / AWS SQS]
    Q --> I[Ingestion worker]
    I --> DB[(PostgreSQL)]
    DB --> E[Leased asynchronous evaluators]
    E --> J[Optional relevance judge]
    E --> DB
    DB --> W[Cohort monitoring worker]
    W --> DB
    DB --> D[Authenticated Streamlit dashboard]
```

The existing FastAPI/Postgres/Streamlit architecture is retained. Locally, a persistent SQLite WAL spool separates model traffic from database availability. A dedicated ingestion worker acknowledges events only after a committed, idempotent Postgres insert. AWS deployments can use SQS Standard and a managed dead-letter queue instead of local filesystem storage. Evaluation jobs remain a durable PostgreSQL queue: short claims and expiring leases avoid a second queue's synchronization cost. Remote model calls run outside database transactions.

## Current implementation

- Proxy authentication, separate provider credentials, safe error categories and bounded request/response/stream/concurrency/rate handling. Limits are per process; distributed quotas require a gateway.
- Metadata-only trace storage by default. Redacted capture is an explicit diagnostic option; arbitrary confidential prose cannot be automatically guaranteed safe.
- Durable ingestion acceptance, stable event IDs, duplicate-safe insertion, exponential retries, dead letters, crash recovery, bounded shutdown and queue health signals.
- Versioned evaluation history, deterministic checks, optional sampled relevance judging, operator-controlled references, finite attempts and poison-record handling.
- Streamlit authentication; agent/model/date/status filters, trace inspection, evaluation history, incomplete cost indicators, queue/worker visibility and restricted alert acknowledgement.
- Candidate deterioration alerts for matched agent/model/request-cohort/evaluator configurations, minimum sample sizes and a statistical bound. These alerts identify score changes to investigate; they do not establish model drift or causation.
- Synthetic security, database, streaming, migration, retry, monitoring and evaluator tests, plus an offline end-to-end demo.
- An optional AWS infrastructure proposal and deployment/recovery instructions. No cloud environment has been deployed or validated by this repository's local test results.

## Run the offline stack

Docker Compose with BuildKit is required. The override explicitly enables insecure **local synthetic** mode and redacted capture. All published ports bind to loopback.

```bash
docker compose -f docker-compose.yml -f docker-compose.offline.yml up --build -d --wait
docker compose -f docker-compose.yml -f docker-compose.offline.yml --profile demo run --rm demo
```

The stack starts Postgres, migrations and restricted runtime roles, the mock provider, proxy, ingestion worker, evaluation worker, dashboard and monitor. The demo generates synthetic support tickets and one intentional invalid-model error. Mock scores and token counts are synthetic. The dashboard listens on local port 8501 and the proxy on local port 8000. Stop services with `docker compose -f docker-compose.yml -f docker-compose.offline.yml down`; this preserves database and spool volumes.

Do not expose the offline stack publicly. Do not use real provider keys, patient records, dealership customer information or confidential documents in this mode.

## Development and tests

Use Python 3.12 and a local Postgres 16 instance whose test role may create and drop temporary databases.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt
export TEST_DATABASE_URL=postgresql://agenteval:agenteval@127.0.0.1:5432/agenteval
pytest
scripts/e2e_offline.sh
```

The DSN above is the deliberately synthetic local Compose administrator. Application containers use separate restricted roles. Tests create databases with generated names and remove only those test databases. A skipped database suite is not successful validation.

For a repeatable simulated automotive service-workflow incident:

```bash
python examples/automotive_demo.py --samples 60 --output artifacts/automotive
```

This scenario creates a temporary loopback database and deterministic model fixture. A retrieval tool reads a fictional work-order routing document; a configuration fault produces an incorrect routing answer; reference evaluations and monitoring detect deterioration; restoring the configuration must pass regression assertions. This is a simulated integration, not evidence of any real dealership or company using the platform. See [the demonstration guide](docs/DEMO.md).

Run `python bench/overhead.py` for measurements on your machine. Historical demo figures are not capacity promises for the hardened version. [Validation evidence](docs/VALIDATION.md) records the commands, actual results and remaining gaps for this implementation.

## Configuration and operating boundaries

| Setting | Purpose |
|---|---|
| `PROXY_API_KEY` | Strong deployment access credential; distinct from provider key |
| `DASHBOARD_PASSWORD` | Independent strong dashboard credential; cloud SSO remains recommended |
| `ALLOW_INSECURE_LOCAL` | Explicit `true` only for isolated synthetic development |
| `UPSTREAM_BASE`, `UPSTREAM_API_KEY` | Fixed provider origin and its credential; deployment requires HTTPS |
| `OPENAI_API_KEY` | Only used for the explicit official OpenAI origin |
| `TRACE_CONTENT_MODE` | `metadata` default; `redacted` opt-in; no raw-content mode |
| `TRACE_REDACT_PATTERNS` | Operator-configured patterns in the supported safe regex syntax |
| `TRACE_QUEUE_BACKEND` | `sqlite` local persistent filesystem or `sqs` existing cloud queue |
| `TRACE_SPOOL_PATH` | Retained local spool path; not a distributed filesystem |
| `TRACE_INGEST_EMBEDDED` | Native development consumer; Compose uses a separate worker |
| `TRACE_SQS_QUEUE_URL` | Pre-existing SQS queue with encryption, retention and DLQ configuration |
| `TRACE_QUEUE_CAPACITY`, `TRACE_QUEUE_MAX_BYTES` | Local backlog limits; SQS gauges are approximate |
| `JUDGE_BASE_URL`, `JUDGE_API_KEY`, `JUDGE_MODEL` | Relevance judge endpoint, credential and model |
| `JUDGE_SAMPLE_RATE`, `JUDGE_MAX_TOKENS` | Stable judge sampling and per-call output limit; not a prepaid spend ceiling |
| `EVAL_REFERENCE_CATALOG` | Operator-owned catalog resolving a caller's `reference_id` |
| `RETENTION_DATABASE_URL`, `TRACE_RETENTION_DAYS` | Restricted maintenance configuration; deletion CLI defaults to dry-run |

Use a separate deployment per organization. An `x-agent-id` label is a filter, not customer authorization. Never rely on labels or dashboard filters for tenant isolation.

Client integration retains the familiar base-URL change:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://your-approved-private-proxy/v1",
    api_key="read-from-your-secret-manager",
    default_headers={"x-agent-id": "service-workflow"},
)
```

For non-streamed responses, `x-trace-delivery: durable` means the ingestion queue accepted the trace, not that evaluation completed. A queue failure can return 503 after the provider already ran; repeating that request can incur another provider charge. Streams use `x-trace-delivery: deferred` because headers precede final capture. A proxy crash before stream finalization can lose that unfinished trace. Inspect trace rejection counters and incomplete outcomes; delivery is not atomic with model execution.

## Documentation

- [Audit, reproduction evidence and prioritized plan](docs/AUDIT.md)
- [Architecture and tradeoffs](docs/DESIGN.md)
- [Threat model and security limitations](docs/THREAT_MODEL.md)
- [Container scan results and release blockers](docs/CONTAINER_SECURITY.md)
- [Evaluation methodology](docs/EVALUATIONS.md)
- [API and trace-delivery contract](docs/API.md)
- [Deployment instructions](docs/DEPLOYMENT.md)
- [AWS, Google Cloud, Azure and simpler staging choices](docs/PLATFORM_OPTIONS.md)
- [Operations, recovery and rollback](docs/OPERATIONS.md)
- [Simulated customer demonstration](docs/DEMO.md)
- [Validation and measured results](docs/VALIDATION.md)

Production acceptance additionally requires an approved deployment scope, real cloud identity/TLS/network checks, provider calibration, load capacity, verified backup restoration and operational ownership. Infrastructure files and passing local tests do not satisfy those gates. No HIPAA or other regulatory compliance is claimed.

MIT License.
