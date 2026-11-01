# AgentEval Proxy

[![CI](https://github.com/Bala-vikram8/Agent-Eval-Proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Bala-vikram8/Agent-Eval-Proxy/actions/workflows/ci.yml)

A local, OpenAI-compatible proxy that forwards an agent's model requests and records best-effort traces for `POST /v1/chat/completions`, including streamed responses, in Postgres. A background worker evaluates **answer relevance** (whether a text reply addresses the user's request), not factual correctness. A Streamlit dashboard shows error rates, latency, time to first token, model costs, and evaluation results. To use the proxy, an OpenAI-compatible agent changes its base URL. **This is a local engineering demo, not a production-ready safety or monitoring service.**

![AgentEval dashboard](screenshots/dashboard.png)

*The dashboard after one run of the offline demo. Offline mode uses a mock model and a stub judge, so the replies and scores in this picture are synthetic. The low relevance example is a canned off topic answer that the mock returns on purpose.*

## How it works

```mermaid
flowchart LR
    A[Agent] -->|OpenAI API calls| P[Proxy<br/>FastAPI]
    P -->|forwarded| U[OpenAI or any<br/>compatible upstream]
    P -.->|trace, after the response| DB[(Postgres)]
    DB <-->|claim trace, save score| W[Eval worker]
    W -->|grading prompt| J[LLM judge]
    DB -->|SQL views| D[Streamlit dashboard]
```

* **Proxy.** Forwards requests under `/v1/` and attempts to record each `POST /v1/chat/completions` call: messages, response, status, token counts, latency and, for streams, time to first token. Streams reach the caller event by event while the proxy reconstructs the reply, including tool calls. Trace writes run in a separate thread after a non-streamed response is read or a stream ends. If Postgres is unavailable, requests continue but traces may be lost.
* **Eval worker.** Asynchronously grades answer relevance (does the text reply address the latest user request?) using a judge model at temperature 0 with JSON output. This does **not** establish that a reply is true, safe, or grounded in evidence. Persisted traces become `scored`, `skipped` with a reason (such as an upstream error or tool-only turn), or `failed` after three judge attempts with backoff. Workers claim rows with `FOR UPDATE SKIP LOCKED` so several can share the queue.
* **Cost in SQL.** Cost is never stored. Views join token counts to a `model_prices` table, so fixing a price corrects history, and a model without a price shows as unpriced instead of being priced at the wrong rate. The judge's own spend is tracked the same way.
* **Schema.** `sql/init.sql` is idempotent and runs on every start, so databases from earlier versions upgrade in place.

[docs/DESIGN.md](docs/DESIGN.md) explains each decision and its tradeoffs.

## What this demo does and does not do

* **After-the-fact evaluations:** The worker scores stored replies after the agent has already received them. It does not block an incorrect answer or prevent it from reaching a user.
* **No automatic drift detection or alerts:** The dashboard exposes relevance scores and operational metrics for manual inspection, but it does not currently detect metric degradation over time or send notifications.
* **Best-effort tracing:** Writes are submitted to an in-process thread pool, not a durable queue. A database outage or an abrupt proxy shutdown can permanently lose traces.
* **Local and synthetic data only:** The demo uses a mock upstream and synthetic tickets. Actual messages and responses are stored without sensitive-data redaction, role-based access, or a retention policy. Do not use real patient information, dealership customer data, or other confidential data in this version; do not expose the local services to the public internet.

## Quick start

Offline, no API key needed. A mock upstream plays the model and the judge:

```bash
docker compose -f docker-compose.yml -f docker-compose.offline.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.offline.yml --profile demo run --rm demo
```

Against OpenAI:

```bash
cp .env.example .env            # then set OPENAI_API_KEY
docker compose up --build -d
docker compose --profile demo run --rm demo
```

Open the dashboard at http://localhost:8501. The demo is a small LangGraph support agent: for each of three tickets it classifies the ticket, may call a `lookup_account` tool, and streams a reply. It finishes with one request to a model that does not exist, so error tracking has something to show.

To trace your own agent, point its client at the proxy:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")  # the proxy adds the real key
```

`make offline`, `make offline-demo`, `make up`, `make demo` and `make down` wrap these commands.

## Configuration

Set these in `.env`. Every published port binds to 127.0.0.1.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | | Used for api.openai.com only, by the proxy and the judge |
| `UPSTREAM_BASE` | `https://api.openai.com` | Any OpenAI compatible server |
| `UPSTREAM_API_KEY` | | Key for a non OpenAI upstream. With no key configured, callers' own keys pass through |
| `PROXY_API_KEY` | | When set, callers must send it as their API key. The proxy swaps it for the upstream key |
| `JUDGE_MODEL` | `gpt-4o-mini` | Judge model. It must support temperature 0 and JSON mode |
| `JUDGE_BASE_URL`, `JUDGE_API_KEY` | | Point the judge at another server |
| `DEMO_MODEL` | `gpt-4o-mini` | Model the demo agent uses |

The schema ships a price for `gpt-4o-mini` only. Add every model you call, in USD per million tokens:

```bash
docker compose exec postgres psql -U agenteval -c \
  "INSERT INTO model_prices (model, input_usd_per_mtok, output_usd_per_mtok) VALUES ('your-model', 1.00, 4.00)"
```

## Measured

What the proxy adds, from `bench/overhead.py`: the mock upstream, the proxy, Postgres and the load generator all on one 2 vCPU Linux VM.

| Scenario | Direct p50 / p95 | Through proxy p50 / p95 | Throughput direct / proxy |
|---|---|---|---|
| Sequential, upstream answers instantly | 1.1 / 1.5 ms | 3.6 / 6.4 ms | |
| 50 concurrent clients, upstream takes 100 ms | 105 / 346 ms | 313 / 1,303 ms | 354 / 104 req/s |

* One request at a time, the proxy adds about 2.5 ms at p50 and 5 ms at p95.
* Under 50 concurrent clients this small VM runs out of CPU at about 100 requests per second through the proxy. The version of this proxy before the rewrite managed 17 requests per second with a p50 of 3 seconds in the same scenario, because it opened a new HTTP client and wrote to Postgres synchronously inside the event loop.
* All 3,050 proxied requests in the benchmark were recorded.

Numbers depend on the machine; rerun the script on yours.

## Tests

`pytest` runs 30 tests against a real Postgres, with the proxy and the mock upstream as separate processes: streaming that reaches the client incrementally, a client hanging up mid stream, streamed tool calls, auth and key handling, upstream and database outages, judge retries and the attempt cap, two workers sharing the queue, upgrades from both earlier schemas, and the dashboard rendered headlessly. CI also builds every Docker image and runs the offline demo end to end (`scripts/e2e_offline.sh`).

```bash
pip install -r requirements-dev.txt
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres  # tests create and drop their own databases
pytest
python bench/overhead.py
scripts/e2e_offline.sh
```

## Limitations

* Only chat completions are recorded. The Responses API, embeddings and other endpoints pass through without a trace.
* The judge measures relevance, **not factual accuracy, hallucinations, policy compliance, or source faithfulness**. Its score is not a safety guarantee.
* The response being judged is part of the judge input and can attempt to manipulate the score.
* No automated drift baselines, alerts, or user-facing quality gates are implemented.
* Messages and responses are stored without redaction or a retention policy. Database and dashboard access controls are not implemented; published Docker ports bind to localhost for demo use.
* In-process trace writes have no durability guarantee, bounded queue, or recovery mechanism if a database is down or the proxy stops unexpectedly.

## Before deploying to the cloud

This repository is **not ready for production traffic or sensitive customer data**. Planned engineering work, in priority order:

1. **Privacy and access:** redact or minimize sensitive data before storage, implement retention and deletion, authenticate the dashboard and API, isolate customer data, and manage secrets and encryption.
2. **Reliable trace ingestion:** use a bounded, durable queue with backpressure, retries, idempotent processing, and visibility into dropped or delayed traces.
3. **Higher-quality evaluation:** add task-specific correctness and groundedness checks backed by references, realistic labeled test cases, prompt-injection resilience testing, and configurable evaluation sampling.
4. **Operational monitoring:** add per-agent baselines and drift alerts, queue-depth and worker-health monitoring, structured logs and traces, and incident procedures.
5. **Cloud delivery:** deploy containerized components with managed Postgres and a durable queue, private networking and least-privilege access, automated CI deployment, backup and recovery, load testing, and cost limits.

All of these are future tasks, not features already implemented. Use synthetic test data during development.

## Related tools

For production use, [LiteLLM](https://docs.litellm.ai/), [Helicone](https://docs.helicone.ai/) and [Langfuse](https://langfuse.com/) cover far more providers and features. This project is a small, readable implementation of the same gateway pattern, built to show how the pieces fit: a streaming relay, logging kept off the request path, an evaluation queue in Postgres, and cost derived in SQL.

## Layout

| Path | What it is |
|---|---|
| `proxy/` | FastAPI proxy: `main.py` request path, `streaming.py` stream parser, `store.py` trace writer |
| `eval/eval_engine.py` | Judge worker |
| `dashboard/app.py` | Streamlit dashboard |
| `sql/init.sql` | Schema, upgrades, prices and views |
| `demo_agent/demo.py` | LangGraph support agent that generates traffic |
| `mock_upstream/server.py` | Offline stand in for the OpenAI API, used by the demo, tests and benchmark |
| `tests/`, `bench/`, `scripts/` | Test suite, overhead benchmark, Docker end to end check |

MIT License.
