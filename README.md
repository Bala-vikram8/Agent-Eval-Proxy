# AgentEval Proxy

[![CI](https://github.com/Bala-vikram8/Agent-Eval-Proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Bala-vikram8/Agent-Eval-Proxy/actions/workflows/ci.yml)

A local, OpenAI compatible proxy that records every chat completion an agent makes, streamed or not, in Postgres. A background worker grades each reply with an LLM judge, and a Streamlit dashboard shows error rate, latency percentiles, time to first token, cost per model, and what the judge itself costs. The agent only changes its base URL.

![AgentEval dashboard](screenshots/dashboard.png)

*The dashboard after one run of the offline demo. Offline mode uses a mock model and a stub judge, so the replies and scores in this picture are synthetic. The low relevance example is a canned off topic answer that the mock returns on purpose.*

## How it works

```mermaid
flowchart LR
    A[Agent] -->|OpenAI API calls| P[Proxy<br/>FastAPI]
    P -->|forwarded| U[OpenAI or any<br/>compatible upstream]
    P -.->|trace, written after the response| DB[(Postgres)]
    W[Eval worker] -->|claims pending traces| DB
    W -->|grading prompt| J[LLM judge]
    D[Streamlit dashboard] -->|SQL views| DB
```

* **Proxy.** Forwards every request and records each `POST /v1/chat/completions` call: messages, response, status, token counts, latency and, for streams, time to first token. Streams reach the caller event by event while the proxy rebuilds the full reply, tool calls included, for the log. Traces are written on a separate thread after the response is sent, and a database outage never fails a request.
* **Eval worker.** Grades answer relevance (does the reply address the request?) with a judge model at temperature 0 and JSON output. Every trace ends `scored`, `skipped` with a reason (upstream error, or a turn that only calls a tool), or `failed` after three attempts with backoff. Workers claim rows with `FOR UPDATE SKIP LOCKED`, so several can share the queue.
* **Cost in SQL.** Cost is never stored. Views join token counts to a `model_prices` table, so fixing a price corrects history, and a model without a price shows as unpriced instead of being priced at the wrong rate. The judge's own spend is tracked the same way.
* **Schema.** `sql/init.sql` is idempotent and runs on every start, so databases from earlier versions upgrade in place.

[docs/DESIGN.md](docs/DESIGN.md) explains each decision and its tradeoffs.

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
* Relevance is not correctness. The judge checks that a reply addresses the request, not that it is true.
* The reply under test sits inside the judge prompt, so a reply can try to steer its own score.
* Full message history is stored on every call and there is no retention policy yet.
* A trace still queued for writing is lost if the proxy is killed.
* The dashboard and Postgres have no authentication; they are only safe because they bind to localhost.

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
