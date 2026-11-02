# Proxy and delivery API

## Authentication

Set an independently generated deployment key of at least 32 characters in `PROXY_API_KEY`. Send `Authorization: Bearer <proxy-key>`. The proxy replaces it with a separate provider credential; the proxy key never goes upstream. Secure mode requires HTTPS to the provider and enables `GET /v1/models` and `POST /v1/chat/completions`. Other methods/endpoints return 403. Explicit insecure-local mode is only for isolated synthetic development.

The dashboard uses an independent strong credential. Cloud access should additionally use a private/SSO boundary. A shared deployment password is not customer identity or tenant isolation.

## Chat completions

Ordinary OpenAI-compatible chat completion JSON and streams are supported. Optional `x-agent-id` must match `[A-Za-z0-9_.:-]{1,64}`. Labels do not authorize access. Invalid shape, compression or labels receive controlled 4xx responses. Configured size/concurrency/rate limits reject overload.

Only chat completions are traced. Responses, embeddings and other API families are not recorded endpoints. Model prices are operator configuration, not a live price-feed guarantee.

Proxy-only context is a string metadata field:

```python
metadata = {"evaluation_context": '{"reference_id":"operator-catalog-entry-v1"}'}
```

The proxy removes it before contacting the model. Client trust flags are ignored. Evaluation resolves the reference from an operator-owned catalog. Metadata-only capture omits content/context and cannot run content-dependent checks. Review reference/evidence configurations for privacy too.

## Outcomes

| Signal | Meaning |
|---|---|
| `x-trace-id` | Proxy event ID for trace search and sink idempotency, separate from provider request ID |
| `x-trace-delivery: durable` | Non-stream trace accepted into the queue; not necessarily inserted or evaluated |
| `x-trace-delivery: deferred` | Streaming headers precede final capture; inspect rejection metrics afterwards |
| 401 / 403 | Authentication failed / endpoint not enabled |
| 413 / 415 | Input too large / encoding unsupported |
| 429 / 503 | Rate, concurrency or trace acceptance unavailable; provider execution may already have completed |
| 502 | Provider transport failure or response limit |

Provider error bodies become safe messages with a bounded allowlisted code. Normal successful replies are forwarded; only minimized/redacted content is persisted. Tool-only turns, incomplete streams and missing content are not completed relevance evaluations. HTTP success is not evidence of task success. Retrying after provider execution can incur another charge.

## Health and metrics

- `/health`: liveness, not proof that dependencies work.
- `/ready`: ingestion inspection/admission, not full system health.
- `/metrics`: authenticated JSON request/rejection/capture counters and ingestion signals. SQS depths are approximate. Process counters reset on restart; queue counters have different lifetimes.

Production needs external collection, alert delivery and data-gap investigation. Never attach credentials, exception bodies, raw headers or trace content to health endpoints.

Cost displays use observed token usage and configured prices; they are not a provider billing ledger. Timed-out, cancelled or failed attempts may incur charges without returning usage. Include those unknowns and retries when reconciling a real provider bill.
