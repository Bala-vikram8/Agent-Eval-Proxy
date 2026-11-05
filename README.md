# AgentEval Proxy

[![CI](https://github.com/Bala-vikram8/Agent-Eval-Proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Bala-vikram8/Agent-Eval-Proxy/actions/workflows/ci.yml)

A local, OpenAI-compatible proxy that sits between an AI agent and its model provider. It forwards every request unchanged, records a trace of each chat completion, and hands the trace to a background worker. The worker grades two things after the fact:

* **Relevance:** does the reply address what the user asked?
* **Grounding:** opt-in per request. Is every claim in the reply supported by the evidence the agent's tools returned?

Replies that fail the grounding check go to a human review queue in the dashboard. To use it, an agent changes its base URL.

**This is an engineering demo that runs locally on synthetic data. It is not a production service, it is not HIPAA compliant, and it has not been validated with a real LLM judge.** [docs/ENGINEERING_REPORT.md](docs/ENGINEERING_REPORT.md) has the full status and the evidence behind every claim below.

![AgentEval dashboard](screenshots/dashboard.png)

*The dashboard after the offline end to end run: a mock model, a stub judge and synthetic data, so the scores are not quality measurements.*

![Human review queue](screenshots/review.png)

*A flagged denial investigation. The agent recommended an appeal citing a 180 day filing limit and a $430.00 rate, but the evidence it had says 90 days and contains no $430.00. The reviewer sees why it was flagged, the reply, and the redacted evidence.*

## Architecture

```mermaid
flowchart LR
    A[Agent] -->|OpenAI API| P[Proxy<br/>FastAPI]
    P -->|forwarded unchanged| U[OpenAI or any<br/>compatible upstream]
    P -->|redacted trace| S[(Local spool<br/>SQLite WAL)]
    S -->|drainer thread,<br/>retries, idempotent| DB[(Postgres)]
    DB <-->|lease, judge, save| W[Eval worker]
    W -->|relevance and<br/>grounding prompts| J[LLM judge]
    W -->|flagged verdicts| R[review_items]
    DB -->|SQL views| D[Streamlit dashboard]
    R --> D
```

| Component | What it does | Code |
|---|---|---|
| Proxy | Relays requests and server sent events to the caller as they arrive, rebuilds streamed replies including tool calls, redacts PII, assigns a trace id, and writes the trace to a local spool before it returns | `proxy/main.py`, `proxy/streaming.py`, `proxy/redaction.py` |
| Trace store | A bounded SQLite spool on a Docker volume. A drainer thread copies rows to Postgres in batches using `ON CONFLICT (trace_id) DO NOTHING`, backs off while Postgres is down, and sends rows Postgres rejects to a dead letter table | `proxy/store.py` |
| Eval worker | Claims traces with a time limited lease, calls the judge outside any transaction, and saves the result only if it still owns the lease. Runs relevance on every trace and grounding on traces that ask for it | `eval/eval_engine.py`, `eval/grounded.py` |
| Review workflow | Opens one review per flagged evaluation. Decisions go through a state machine with a version check, and every change is written to an append only audit table | `dashboard/reviews.py`, `dashboard/review_cli.py` |
| Dashboard | Shows traffic, cost, latency, ingestion health, worker heartbeats, evaluations and the review queue, all computed by SQL views | `dashboard/app.py`, `sql/init.sql` |
| Denial demo | A synthetic claim denial investigation agent built with LangGraph, kept apart from the core code | `demos/rcm_denial/` |

[docs/DESIGN.md](docs/DESIGN.md) explains each decision, the reliability guarantees, and the failure cases that remain.

## Quick start

Offline, with no API key. A mock upstream plays the model and the judge:

```bash
make offline            # docker compose -f docker-compose.yml -f docker-compose.offline.yml up --build -d
make offline-demo       # generic support agent, 3 tickets plus one deliberate error
make offline-rcm-demo   # synthetic denial investigations with grounding checks
```

Open http://localhost:8501. The review queue fills in a few seconds.

Against OpenAI. This sends synthetic prompts to OpenAI and incurs a small charge on your key:

```bash
cp .env.example .env    # set OPENAI_API_KEY, and set PROXY_API_KEY and DASHBOARD_PASSWORD
make up && make rcm-demo
```

To trace your own agent:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="your PROXY_API_KEY")
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=messages,
    extra_headers={
        "X-AgentEval-Case-Id": "CLM-24-10047",     # optional, groups traces in reviews
        "X-AgentEval-Evaluations": "grounded",     # opt in to the grounding check
    },
)
```

The proxy validates and removes both `X-AgentEval-*` headers before forwarding, and returns `X-AgentEval-Trace-Id` on every recorded response.

Reviews can be decided in the dashboard or from the command line:

```bash
docker compose exec dashboard python review_cli.py list
docker compose exec dashboard python review_cli.py decide 1 reject --actor alex --note "rate not in contract"
```

## Evaluation methodology

There are two layers, and each grounding result records which layer produced it:

1. **Deterministic identifier check.** It extracts money amounts, CARC and RARC codes, claim ids, dates, day counts and CPT codes from the reply and the evidence, normalizes them (for example `$430` and `430.00 USD` match, `2026-02-20` and `Feb 20, 2026` match), and flags any reply identifier missing from the evidence. It runs with no model and no network.
2. **LLM claim labeling.** The judge splits the reply into claims and labels each one `supported`, `contradicted` or `unsupported`, citing evidence numbers. It also notes required items the reply left out. The reply and evidence are passed as JSON values that the prompt says are data, which makes prompt injection harder but does not prevent it.

The verdict follows a fixed order: `contradicted`, then `insufficient_evidence`, then `unsupported`, then `incomplete`, then `supported`. A reply with no evidence at all gets `insufficient_evidence` without calling the judge. Every verdict except `supported` opens a review (configurable with `GROUNDED_REVIEW_VERDICTS`).

**Labeled dataset.** `eval/datasets/grounded_rcm_v1.json` has 24 synthetic cases: supported, unsupported, contradicted (including two that cite a superseded policy version), insufficient evidence, incomplete, ambiguous, and prompt injection. One person labeled them, and that person also wrote the evaluator, so they are a regression set, not an independent benchmark.

| Judge | Correct verdicts, 21 unambiguous cases | Review recall | Missed (false negatives) | Flagged wrongly (false positives) |
|---|---|---|---|---|
| Deterministic layer only | 8 / 21 | 0.529 | U2, U4, C3, C4, N1, N2, N3, J2 | S2 |
| Mock heuristic stub | 8 / 21 | 0.941 | N2 | S2, S3, S4, S5 |
| Real LLM judge | **not run**: no API key was available | | | |

Reproduce with `make dataset` and `python eval/run_dataset.py --judge mock --base-url http://127.0.0.1:9911/v1` (mock on that port). Full reports are in `eval/results/`.

How to read the table:

* The **deterministic** row is a real measurement of a real component. It catches invented identifiers and misses anything that needs reading comprehension: wrong reasoning, missing items, or a superseded policy cited with its correct identifiers.
* The **mock** row measures a keyword overlap stub that exists so the pipeline runs offline. It is not a quality number for anything.
* **Accuracy with a real judge is unknown.** Run `make dataset JUDGE=openai` with a key to measure it. Small numbers like these would also need a larger, independently labeled set before they could support decisions.

## Reliability

Verified by tests and drills in this repository:

* A trace is on disk in the spool before the response returns to the caller. It survives a Postgres outage and a proxy restart, and arrives in Postgres exactly once. `scripts/outage_drill.sh` stops the real Postgres container under traffic, restarts the proxy while 20 traces are spooled, starts Postgres again, and checks all 25 traces landed once. The longest ingestion delay in the last run was 21 seconds.
* The spool is bounded (`TRACE_SPOOL_MAX_ROWS`, default 50,000). When it is full, new traces are dropped and counted, requests keep working, and `/health/ready` returns 503.
* A row Postgres rejects goes to `dead_letter` after 3 attempts instead of blocking the batch.
* Eval workers hold a lease, not a database lock, while they wait on the judge. A worker that crashes loses its lease and the row is retried. A worker that resumes after its lease expired cannot overwrite the new owner's result.
* Upstream timeouts return 504. An upstream that fails mid stream ends with a final event carrying an `error` object, which the OpenAI SDKs raise on, and a trace with `error_type`.

Not covered: losing the spool volume itself, running several proxy replicas against one spool, and disk full on the spool volume.

## Observability

* `/health/live` reports whether the process is up. `/health/ready` reports degraded while Postgres is down and 503 when the spool is full.
* `/metrics` exports Prometheus text: spool depth, spooled, written, dropped and dead lettered totals, and drain failures. It requires `PROXY_API_KEY` when one is set.
* Logs are JSON lines with trace ids, statuses and latencies. They never contain message content or keys (checked in the end to end script).
* The dashboard shows p95 ingestion delay, client disconnects, eval backlog and expired leases, and worker heartbeats, with alerts when a worker goes quiet or the backlog ages.

## Security boundaries

Implemented: every published port binds to 127.0.0.1; optional proxy key and dashboard password; `OPENAI_API_KEY` is only ever sent to api.openai.com; pattern redaction of SSNs, emails, phone and card numbers before anything is stored; a request size limit; an opt-in retention purge; dependency audit in CI.

Not implemented: per user accounts or roles, TLS, encryption at rest beyond the host's disk, redaction of names, addresses or free text identifiers, tenant isolation, or a BAA with any model provider. [docs/SECURITY.md](docs/SECURITY.md) lists what remains before regulated data could be considered. Use synthetic data only.

## Configuration

Set these in `.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | | Used only for api.openai.com, by the proxy and the judge |
| `UPSTREAM_BASE`, `UPSTREAM_API_KEY` | `https://api.openai.com` | Any OpenAI compatible server and its key |
| `PROXY_API_KEY` | | Callers must send it as their API key. Also protects `/metrics` |
| `DASHBOARD_PASSWORD` | | Shared password for the dashboard |
| `REDACT_PII` | `true` | Pattern redaction before storage |
| `RETENTION_DAYS` | `0` (keep) | Worker deletes traces, evaluations and reviews older than this |
| `TRACE_SPOOL_MAX_ROWS` | `50000` | Spool bound |
| `UPSTREAM_TIMEOUT_SECONDS`, `MAX_REQUEST_BYTES` | `300`, 10 MiB | Request limits |
| `JUDGE_MODEL`, `JUDGE_BASE_URL`, `JUDGE_API_KEY` | `gpt-4o-mini` | Judge, which must support temperature 0 and JSON mode |
| `JUDGE_MAX_ATTEMPTS`, `JUDGE_TIMEOUT_SECONDS`, `EVAL_LEASE_SECONDS` | `3`, `60`, `120` | Judge retry and lease limits |
| `GROUNDED_REVIEW_VERDICTS` | all except `supported` | Which grounding verdicts open a review |

The schema ships a price for `gpt-4o-mini` only. Cost is computed in SQL from the model the provider says served the request, so add any model you use:

```bash
docker compose exec postgres psql -U agenteval -c \
  "INSERT INTO model_prices (model, input_usd_per_mtok, output_usd_per_mtok) VALUES ('your-model', 1.00, 4.00)"
```

## Measured overhead

`bench/overhead.py`, with the mock upstream, the proxy, Postgres and the load generator on one 4 vCPU x86_64 Linux VM:

| Scenario | Direct p50 / p95 | Through proxy p50 / p95 | Throughput direct / proxy |
|---|---|---|---|
| Sequential, upstream answers instantly | 0.4 / 0.5 ms | 1.8 / 2.2 ms | |
| 50 concurrent clients, upstream takes 100 ms | 102.0 / 118.1 ms | 105.7 / 134.2 ms | 467 / 441 req/s |

All 3,050 proxied requests were recorded. The durable spool costs about 1 to 2 ms more at p50 under concurrency than the previous in-memory writer, measured on the same VM. Numbers depend on the machine; rerun the script on yours.

## Tests

103 tests in `pytest`, run against a real Postgres with the proxy and mock upstream as separate processes:

| File | Tests | Covers |
|---|---|---|
| `test_reliability.py` | 23 | Spool durability, Postgres outage through a TCP relay, process termination, idempotency, dead letters, bounds, concurrent streams, timeouts, mid stream failure, header handling, redaction, health, metrics |
| `test_grounded.py` | 29 | Identifier canonicalization, verdict order, judge parsing, dataset checks |
| `test_eval.py` | 16 | Relevance worker, retries and attempt cap, leases and stale workers, poison rows, no open transaction during the judge call, judge token accounting, injection, retention |
| `test_proxy.py` | 11 | Forwarding, auth and key rules, streaming, model attribution |
| `test_reviews.py` | 9 | State machine, stale versions, duplicate prevention, append only audit |
| `test_dashboard.py` | 7 | Headless render, numbers checked against independent SQL, review form, password gate |
| `test_streaming.py`, `test_migrations.py`, `test_rcm_demo.py` | 4, 3, 1 | Stream parsing, upgrades from both earlier schemas, the denial demo through the proxy |

```bash
pip install -r requirements-dev.txt
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres  # tests create and drop their own databases
pytest
make e2e      # builds every image and runs both demos through the offline stack
make drill    # Postgres outage and proxy restart under traffic
make audit    # pip-audit on every requirements file
```

CI runs all of these on every push.

### Apple Silicon

The base images (`python:3.12-slim`, `postgres:16`) are published for arm64, and every pinned dependency has an arm64 Linux wheel. In an emulated arm64 container (QEMU on x86), all requirements installed, every module imported, the dataset run gave the same results, and the proxy served `/health/live` and `/health/ready`. **The full stack has not been built or run on a real Apple Silicon Mac.**

## Completed and planned

Completed in this repository, with tests: durable trace ingestion, model attribution using the provider's reported model, grounded evaluation with a labeled dataset, human review with an audit trail, redaction, retention, health and metrics endpoints, structured logs, the denial demo, and the outage drill.

Planned, not implemented:

1. Measure the grounding judge with a real model, and get the dataset labeled by a second, independent person.
2. Per user authentication and roles for the dashboard and review actions, plus TLS.
3. Stronger de-identification (names, addresses, free text) before anything reaches a model provider or the database.
4. Pre-response gating, so a flagged reply can be held before it reaches a user. Today every check is retrospective.
5. Drift baselines per agent and alert delivery. The dashboard only shows alerts on screen.
6. Recording the Responses API, which is forwarded but not traced today.
7. Cloud deployment. [docs/CLOUD_ASSESSMENT.md](docs/CLOUD_ASSESSMENT.md) is a proposal only; nothing has been deployed.

## Layout

| Path | What it is |
|---|---|
| `proxy/` | Request path, stream parser, trace spool and drainer, redaction |
| `eval/` | Worker, grounding evaluator, dataset runner, labeled dataset and reports |
| `dashboard/` | Streamlit dashboard, review state machine and CLI |
| `sql/init.sql` | Idempotent schema, upgrades, prices and views |
| `demo_agent/` | Generic LangGraph support agent |
| `demos/rcm_denial/` | Synthetic denial investigation demo |
| `mock_upstream/` | Offline stand in for the OpenAI API |
| `tests/`, `bench/`, `scripts/` | Tests, overhead benchmark, end to end check and outage drill |
| `docs/` | Audit, design, security, evaluation report, demo script, interview prep, cloud assessment |

For production use, [LiteLLM](https://docs.litellm.ai/), [Helicone](https://docs.helicone.ai/) and [Langfuse](https://langfuse.com/) cover far more providers and features. This project is a small, readable implementation of the same gateway pattern.

MIT License.
