# Design notes

Why the code is shaped the way it is, what each choice costs, what is guaranteed, and what can still go wrong.

## Request path

1. The agent calls `http://localhost:8000/v1/...` with any OpenAI client. Only the base URL changes.
2. If `PROXY_API_KEY` is set, the caller must present it. The proxy then sets the upstream `Authorization` header (rules below) and forwards the request with its query string. Connection level headers and the proxy's own `X-AgentEval-*` headers are removed. The body is untouched except for one flag on streamed requests, explained below.
3. `X-AgentEval-Case-Id` (1 to 64 characters from a fixed set) and `X-AgentEval-Evaluations` (only `grounded` today) are validated. A bad value gets a 400 and is never forwarded, so the caller finds out instead of silently losing the evaluation.
4. Anything other than `POST /v1/chat/completions` is forwarded and not recorded. Server sent event responses from other endpoints, such as `/v1/responses`, are relayed as they arrive instead of being buffered.
5. A chat completion is timed from the moment it is sent upstream. A normal response is read, redacted, spooled and returned with `X-AgentEval-Trace-Id`. A stream is relayed event by event and spooled when it ends, fails or the caller disconnects.
6. Request bodies over `MAX_REQUEST_BYTES` get a 413. Upstream timeouts (`UPSTREAM_TIMEOUT_SECONDS`) get a 504, connection failures a 502. All of these are recorded with an `error_type`.

## Streaming

* Usage only arrives in a stream when the request sets `stream_options.include_usage`. The proxy adds that flag so it can record token counts. If the caller did not ask for it, the extra final chunk (empty `choices`, filled `usage`) is removed before the caller sees it, because code that reads `chunk.choices[0]` would crash on it.
* `streaming.StreamAccumulator` splits the byte stream into events, which can arrive split across reads, and rebuilds the final message, including tool calls whose arguments arrive in pieces. Chunks whose `choices` or `delta` are not objects are ignored rather than crashing the relay.
* Time to first token is measured at the first content or tool call delta, not at the first byte, because the first chunk usually carries only the role.
* If the upstream fails mid stream, the caller receives a final event carrying an `error` object, which the OpenAI SDKs raise on, and the trace gets an `error_type` ending in `_mid_stream` (for example `upstream_timeout_mid_stream`) with whatever text arrived before the failure. A stream that ends without its final `[DONE]` marker is recorded as `upstream_incomplete_stream`. If the caller hangs up, the trace is spooled as `client_disconnect` with `stream_completed: false` and the upstream connection is closed inside a shielded cancel scope.

## Model attribution and cost

* `model` is what the caller asked for. `response_model` is what the provider says served it, for example `gpt-4o-mini-2024-07-18`. Providers can alias or upgrade models, so cost is computed from `coalesce(response_model, model)`.
* Cost is never stored. `traces_enriched` joins token counts to `model_prices`, so a price correction fixes history, and an unknown model shows as unpriced instead of being priced at another model's rate. Dated ids fall back to their family price.
* Traces without usage (for example a stream cut off before the usage chunk) are counted in `usage_missing`, so missing cost is visible rather than reported as zero.
* The judge's own tokens are stored and priced, including tokens spent on attempts that failed to parse. The SDK's hidden retries are turned off (`max_retries=0`) so every attempt is visible and counted.

## Trace ingestion

`proxy/store.py` replaces the old in-memory thread pool, which lost every queued trace whenever Postgres was down or the proxy stopped.

### What happens to a trace

1. `submit()` runs in a worker thread (`anyio.to_thread`) so the event loop never touches the disk. It inserts the redacted record into a SQLite spool in WAL mode on the `proxy_spool` Docker volume and returns.
2. A single drainer thread reads up to 100 rows in order, inserts them into Postgres in one transaction with `ON CONFLICT (trace_id) DO NOTHING`, and deletes them from the spool only after the commit.
3. When the proxy stops, it drains for up to 5 seconds (compose gives it 15). Anything left stays in the spool and is sent after the next start.

### Guarantees

| Failure | Result | Evidence |
|---|---|---|
| Postgres down, slow, or restarting | Traces wait in the spool and arrive once it is back. Requests are unaffected. Readiness shows `degraded`. | `test_reliability.py` outage tests through a TCP relay; `scripts/outage_drill.sh` against the real container |
| Proxy killed or restarted with traces spooled | Recovered at next start | Restart test; drill restarts the proxy with 20 traces spooled |
| Crash after the Postgres commit, before the spool delete | Row is sent again; the unique `trace_id` drops the duplicate | Idempotency test |
| One row Postgres rejects | Batch is retried row by row. The bad row moves to a `dead_letter` table in the spool file after 3 attempts and is counted; the other rows land | Dead letter test |
| Spool full | New traces are dropped and counted in `agenteval_traces_dropped_total`, an error is logged, readiness returns 503, requests keep working | Bound test |
| U+0000 in content | Replaced, because Postgres text cannot hold it | Test |

### Remaining failure cases

* **Power loss or OS crash.** `synchronous=NORMAL` does not fsync every commit, so the last few traces before such a crash can be lost. A process crash does not lose them. `synchronous=FULL` would close this gap and cost latency on every request.
* **Losing the spool volume** loses whatever was in it.
* **Disk full** on the spool volume makes `submit()` fail; the trace is dropped and counted.
* **Several proxy replicas** must each have their own spool. Sharing one SQLite file between hosts is not supported.
* **Delivery is at least once into the spool and exactly once into Postgres** only because of the `trace_id` key. Anything reading the spool directly must apply the same rule.

## Eval queue

* Every trace starts as `pending` and ends as `scored`, `skipped` or `failed`. Skipped and failed rows carry the reason in `eval_error`.
* **Claim.** A short transaction marks one row `running`, increments `eval_attempts`, and sets a lease (`EVAL_LEASE_SECONDS`, default twice the judge timeout) and a random claim token. It uses `FOR UPDATE SKIP LOCKED` so workers do not collide. The transaction then commits.
* **Judge.** The worker calls the judge with no transaction open. The previous design held a row lock and an open transaction for the whole judge call, so a slow judge pinned connections and a crashed worker held rows until its connection timed out.
* **Finalize.** The result is written with `WHERE eval_claim_token = <token>`. If the lease expired and another worker reclaimed the row, the stale worker's write matches nothing and is discarded. Tested with two workers and an expired lease.
* **Retries.** A judge error schedules a retry with capped exponential backoff and gives up after `JUDGE_MAX_ATTEMPTS`. Attempts are counted at claim time, so a worker that crashes every time on the same row still exhausts the limit.
* **Poison rows.** An unexpected exception marks that row `failed` and the worker moves on.
* **Grounding** uses the same pattern in `trace_evaluations`, one row per trace and evaluation type, created only for traces that asked for it.
* **Heartbeats.** Each worker upserts `worker_heartbeats` every loop with counts and its last error. The dashboard alerts when the newest heartbeat is older than `ALERT_WORKER_STALE_SECONDS`.
* To requeue failed rows after fixing the judge: `UPDATE traces SET eval_status = 'pending', eval_attempts = 0, eval_next_attempt_at = now() WHERE eval_status = 'failed';`

## Relevance judge

* It measures whether the reply addresses the last user request. It does not check facts.
* The exchange is sent as one JSON object whose string values the prompt calls data, at temperature 0 with JSON output and a strict parser. A malformed verdict is an error, not a silent score.
* Inputs are truncated to 4,000 characters each to cap judge cost.

## Grounded evaluation

`eval/grounded.py`, rubric `grounded-v1`.

* **Evidence** is the tool results in the conversation, numbered `E1`, `E2` and so on. Without evidence the verdict is `insufficient_evidence` and no judge call is made, so cost is not spent grading a reply against nothing.
* **Deterministic layer.** Money, CARC and RARC codes, claim ids, dates, day counts and CPT codes are extracted from the reply and the evidence and normalized to one form each. A reply identifier not in the evidence set is reported. Matching is set membership on normalized values, not substring search, so `$43` does not match `$430.00`.
* **Judge layer.** The judge labels each claim `supported`, `contradicted` or `unsupported` with evidence ids, and lists required items the reply omitted. The parser rejects unknown labels and missing fields. Citations of evidence ids that do not exist are kept apart in `unknown_evidence_ids` instead of counting as support.
* **Aggregation** is code, not the model: contradicted claim → `contradicted`; the judge saying evidence is insufficient → `insufficient_evidence`; any unsupported claim or unmatched identifier → `unsupported`; omissions → `incomplete`; otherwise `supported`. The reasons list always includes unmatched identifiers, so a reviewer sees them whatever the verdict.
* **Known weaknesses.** The deterministic layer cannot see reasoning errors or a superseded policy cited with correct identifiers (cases C3 and C4). The judge sees the reply under test, so prompt injection is mitigated, not prevented. The dataset is small and labeled by one person. See `README.md` for the measured numbers.

## Human review

* `review_items` has one row per flagged evaluation (a unique constraint on `evaluation_id`), so retries and duplicate workers cannot open two reviews.
* States: `pending` → `approved` or `rejected` → `resolved`. Rejecting requires a note. Every transition checks the expected state and a version number in the same `UPDATE`, so two reviewers acting on the same item cannot both succeed; the second gets an error and must reload.
* `review_events` records actor, from and to states, note and time. A trigger rejects any `UPDATE`, so a recorded decision cannot be rewritten. `DELETE` is still allowed, because retention removes a review's events along with the review. Anyone with direct database write access can delete events or drop the trigger, so this protects against application mistakes, not against a malicious database user.
* Review is **retrospective**: the agent's reply has already been returned. The dashboard says so on the review panel.
* The reviewer name is typed in, not authenticated. It is an accountability aid in a single team demo, not an identity control.

## Keys

* `UPSTREAM_API_KEY` is used for the upstream when set.
* `OPENAI_API_KEY` is only used when the upstream is `https://api.openai.com`, so pointing `UPSTREAM_BASE` at another server cannot leak it. The eval worker follows the same rule for the judge.
* With no key configured, the caller's own `Authorization` header passes through.
* Every published port binds to 127.0.0.1. Without that, a proxy that injects a key is an open relay billed to you.

## Data model

* `traces` holds raw JSON (`messages`, `response`, `tool_calls`) next to typed columns for anything aggregated.
* `trace_summary`, `model_summary`, `eval_backlog`, `evaluation_summary` and `review_summary` hold every number the dashboard shows. The dashboard only formats them, and a test checks its numbers against independent SQL.
* `sql/init.sql` is idempotent and runs on every start through the `migrate` service. It upgrades databases from both earlier versions of this project. The new evaluation table is named `trace_evaluations` because the oldest schema already has an unrelated `evaluations` table.
* `purge_traces_older_than(days)` deletes old traces with their evaluations and reviews. The worker calls it hourly when `RETENTION_DAYS` is above zero. Review events for purged reviews are removed with them, which is a deliberate tradeoff between retention and audit and should be revisited for any regulated use.

## Performance

* Sequential overhead is about 1.4 ms at p50, and under 50 concurrent clients about 3.7 ms at p50 on a 4 vCPU VM (README table). The durable spool adds roughly 2 ms at p50 under that load compared with the earlier in-memory writer on the same VM.
* The proxy is CPU bound under heavy concurrency: bodies are parsed as JSON and every call passes through httpx and the ASGI stack in Python.
* Ways to scale: more uvicorn workers or replicas (each with its own spool), or a gateway in Go or Rust when throughput matters more than readability.
