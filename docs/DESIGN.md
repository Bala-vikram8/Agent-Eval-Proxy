# Design notes

Why the code is shaped the way it is, what each choice costs, and what I would change next.

## Request path

1. The agent calls `http://localhost:8000/v1/...` with any OpenAI client. Only the base URL changes.
2. If `PROXY_API_KEY` is set, the caller must present it. The proxy then sets the upstream `Authorization` header (rules below) and forwards the request with its query string. Connection level headers are dropped, and the body is untouched except for one flag on streamed requests, explained below.
3. Anything that is not `POST /v1/chat/completions` is forwarded and never recorded.
4. A chat completion is timed from the moment it is sent upstream. For a normal response the proxy reads the body, returns it, and queues a trace. For a stream it relays each server sent event as soon as the event is complete, and queues the trace when the stream ends or the caller disconnects.

## Streaming

* Usage only arrives in a stream when the request sets `stream_options.include_usage`. The proxy adds that flag so it can record token counts. If the caller did not ask for it, the extra final chunk (empty `choices`, filled `usage`) is removed before the caller sees it, because code that reads `chunk.choices[0]` would crash on it.
* `streaming.StreamAccumulator` splits the byte stream into events, which can arrive split across reads, and rebuilds the final message, including tool calls whose arguments arrive in pieces.
* Time to first token is measured at the first content or tool call delta, not at the first byte, because the first chunk usually carries only the role.
* If the caller hangs up, the generator is cancelled. The trace is queued first and the upstream connection is then closed inside a shielded cancel scope, so a disconnect still produces a trace marked `stream_completed: false`.

## Logging stays off the request path

* Traces are written by `store.TraceStore`, a thread pool with one Postgres connection per thread. The event loop only hands over a dict.
* Observability must not take the application down. If Postgres is unreachable the write is dropped with a warning and requests keep flowing. There is a test for exactly this.
* The cost of that choice: a trace still in the queue is lost if the proxy is killed. A durable local buffer would fix it.
* The earlier version of this proxy opened a new HTTP client and wrote to Postgres synchronously inside the async handler. Under 50 concurrent clients with a 100 ms upstream on a 2 vCPU VM it managed 17 requests per second with a p50 near 3 seconds. The current version, measured the same way, did about 100 requests per second (median of three runs). See the README for the reproducible numbers.

## Keys

* `UPSTREAM_API_KEY` is used for the upstream when set.
* `OPENAI_API_KEY` is only used when the upstream is `https://api.openai.com`, so pointing `UPSTREAM_BASE` at another server cannot leak it. The eval worker follows the same rule for the judge.
* With no key configured, the caller's own `Authorization` header passes through.
* Every published port binds to 127.0.0.1. Without that, a proxy that injects a key is an open relay billed to you.

## Eval queue

* Every trace starts as `pending` and ends as `scored`, `skipped` or `failed`. Skipped and failed rows carry the reason in `eval_error`, so nothing silently disappears.
* A worker claims one row at a time with `SELECT ... FOR UPDATE SKIP LOCKED` inside its own transaction. Several workers can share the queue, and a row that breaks the judge cannot roll back other rows.
* Rows are skipped, not judged, when the upstream returned an error or the reply has no text, for example a turn that only calls a tool.
* A judge error schedules a retry with exponential backoff (`JUDGE_RETRY_BASE_SECONDS`, default 30) and gives up after `JUDGE_MAX_ATTEMPTS` (default 3). An earlier version retried failures every 30 seconds forever, and a crash on tool call turns stopped every older trace from being scored.
* To requeue failed rows after fixing the judge: `UPDATE traces SET eval_status = 'pending', eval_attempts = 0, eval_next_attempt_at = now() WHERE eval_status = 'failed';`

## Judge

* It measures answer relevance: does the reply address the last user request, given the system prompt. It does not check facts. The earlier versions called this faithfulness, which it never was, since the judge never saw any source material.
* Temperature 0, JSON mode, and a parser that rejects missing or out of range scores, so a malformed verdict is an error, not a silent 0.5.
* Inputs are truncated to 4,000 characters each to cap judge cost.
* The judge's own token usage is stored and priced. Grading every call with an LLM can cost as much as the calls themselves, so the dashboard shows both.
* Known weakness: the reply under test is inside the judge prompt, so a reply can try to steer the score. The prompt tells the judge that tagged text is data, which helps but is not a guarantee.

## Data model

* One `traces` table. Raw JSON (`messages`, `response`, `tool_calls`) sits next to typed columns for anything that gets aggregated.
* Cost is never stored. `traces_enriched` joins token counts to `model_prices`, so a price correction fixes history, and an unknown model shows up as unpriced instead of being priced at somebody else's rate. Dated model ids such as `gpt-4o-mini-2024-07-18` fall back to their family price.
* `trace_summary` and `model_summary` hold every aggregate the dashboard shows, using `percentile_cont` for p50 and p95. The dashboard itself only formats numbers.
* `sql/init.sql` is idempotent and runs on every start through the `migrate` service. It upgrades databases created by the two earlier versions of this project: it renames the old faithfulness columns, converts `created_at` to `timestamptz`, keeps existing scores, and drops the stored cost column.

## Performance limits

* Sequential overhead is a few milliseconds (see the README table).
* Under heavy concurrency on a small machine the proxy is CPU bound. Request and response bodies are parsed as JSON and every call passes through httpx and the ASGI stack in Python. Taking the database away did not raise throughput, so the trace writes are not the bottleneck.
* Ways to scale: more uvicorn workers or replicas behind a load balancer, or a gateway in Go or Rust when throughput matters more than readability.

## What I would do next

1. Retention: partition `traces` by month and drop or archive old partitions. Agents resend the whole history every turn, so `messages` grows fast.
2. Redact secrets and personal data before storage.
3. Grade faithfulness against tool results for turns that follow a tool call.
4. Sample the judge (for example 10 percent of traffic) or use a batch API, to cut judge cost.
5. Record the Responses API as well as chat completions.
6. Export spans with the OpenTelemetry GenAI conventions, so traces can flow into existing observability tools.
