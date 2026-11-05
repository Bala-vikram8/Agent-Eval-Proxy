"""AgentEval proxy.

Forwards OpenAI compatible requests to UPSTREAM_BASE unchanged and records every
POST /v1/chat/completions call, streamed or not. Each recorded call gets a trace id, returned
to the caller in the X-AgentEval-Trace-Id header, and is written to a durable local spool
before the proxy lets go of it (see store.py). Recording can never fail the request.
"""
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from redaction import redact
from store import TraceStore
from streaming import StreamAccumulator


class JsonFormatter(logging.Formatter):
    EXTRA = ("trace_id", "status_code", "latency_ms", "model", "response_model", "error_type",
             "spool_depth", "case_ref", "streamed")

    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
                 "logger": record.name, "msg": record.getMessage()}
        entry.update({k: getattr(record, k) for k in self.EXTRA if hasattr(record, k)})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter() if os.getenv("LOG_FORMAT", "json") == "json"
                     else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
log = logging.getLogger("agenteval.proxy")

DEFAULT_UPSTREAM = "https://api.openai.com"
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", DEFAULT_UPSTREAM).rstrip("/")
# OPENAI_API_KEY is only used for the real OpenAI endpoint, so pointing UPSTREAM_BASE
# somewhere else can never leak it. Other upstreams take UPSTREAM_API_KEY.
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY") or (
    os.getenv("OPENAI_API_KEY") if UPSTREAM_BASE == DEFAULT_UPSTREAM else None
)
# Optional shared secret. When set, callers must send it as their API key.
PROXY_API_KEY = os.getenv("PROXY_API_KEY") or None
DATABASE_URL = os.environ["DATABASE_URL"]
SPOOL_PATH = os.getenv("TRACE_SPOOL_PATH", "/var/lib/agenteval/spool.db")
SPOOL_MAX_ROWS = int(os.getenv("TRACE_SPOOL_MAX_ROWS", "50000"))
REDACT_PII = os.getenv("REDACT_PII", "true").lower() not in ("0", "false", "no")
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(10 * 1024 * 1024)))
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "300"))

# Connection level headers are never forwarded. Authorization is rebuilt below.
DROP_REQUEST_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "proxy-connection",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
    "accept-encoding", "authorization",
}
# httpx already decoded the body, so length and encoding headers no longer apply.
DROP_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive",
}
# Headers addressed to the proxy itself. They are read here and never sent upstream.
AGENTEVAL_HEADER_PREFIX = "x-agenteval-"
CASE_REF_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
KNOWN_EVALUATIONS = {"grounded"}

METRICS = {"requests": {}, "upstream_errors": {}}


def count(kind: str, label: str) -> None:
    METRICS[kind][label] = METRICS[kind].get(label, 0) + 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    app.state.store = TraceStore(DATABASE_URL, SPOOL_PATH, max_rows=SPOOL_MAX_ROWS)
    app.state.store.start()
    log.info("forwarding to %s, proxy key %s, redaction %s", UPSTREAM_BASE,
             "required" if PROXY_API_KEY else "not required", "on" if REDACT_PII else "off")
    yield
    await app.state.http.aclose()
    await anyio.to_thread.run_sync(app.state.store.close)


app = FastAPI(title="AgentEval Proxy", lifespan=lifespan)


@app.get("/health")
@app.get("/health/live")
async def health():
    """Liveness: the process is serving HTTP. Says nothing about dependencies."""
    return {"status": "ok"}


@app.get("/health/ready")
async def ready():
    """Readiness: can this instance accept traffic without losing traces?

    Postgres being down does not make the proxy unready, because traces are spooled and
    requests keep working; it is reported as degraded. A full spool does, because new traces
    would be dropped."""
    stats = app.state.store.stats()
    if app.state.store.full:
        status, code = "unready: trace spool full, new traces are being dropped", 503
    elif stats["postgres_ok"] is False:
        status, code = "degraded: Postgres unavailable, traces are spooled locally", 200
    else:
        status, code = "ready", 200
    return JSONResponse(status_code=code, content={"status": status, "trace_store": stats})


@app.get("/metrics")
async def metrics(request: Request):
    if PROXY_API_KEY and not authorized(request.headers.get("authorization")):
        return PlainTextResponse("unauthorized\n", status_code=401)
    stats = app.state.store.stats()
    lines = [
        "# HELP agenteval_requests_total Recorded chat completion requests by outcome.",
        "# TYPE agenteval_requests_total counter",
        *[f'agenteval_requests_total{{outcome="{k}"}} {v}' for k, v in sorted(METRICS["requests"].items())],
        "# TYPE agenteval_upstream_errors_total counter",
        *[f'agenteval_upstream_errors_total{{type="{k}"}} {v}' for k, v in sorted(METRICS["upstream_errors"].items())],
        "# TYPE agenteval_trace_spool_depth gauge",
        f"agenteval_trace_spool_depth {stats['spool_depth']}",
        "# TYPE agenteval_trace_spool_capacity gauge",
        f"agenteval_trace_spool_capacity {stats['spool_capacity']}",
        "# TYPE agenteval_trace_spool_oldest_age_seconds gauge",
        f"agenteval_trace_spool_oldest_age_seconds {stats['oldest_spooled_age_seconds']}",
        "# TYPE agenteval_trace_postgres_up gauge",
        f"agenteval_trace_postgres_up {1 if stats['postgres_ok'] else 0}",
        "# TYPE agenteval_trace_dead_letter_rows gauge",
        f"agenteval_trace_dead_letter_rows {stats['dead_letter_rows']}",
    ]
    for name in ("spooled", "dropped", "written", "dead_lettered", "drain_failures"):
        lines += [f"# TYPE agenteval_traces_{name}_total counter", f"agenteval_traces_{name}_total {stats[name]}"]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


def authorized(incoming: str | None) -> bool:
    return hmac.compare_digest(incoming or "", f"Bearer {PROXY_API_KEY}")


def upstream_authorization(incoming: str | None) -> tuple[bool, str | None]:
    """Decide which Authorization header goes upstream. Returns (allowed, header)."""
    if PROXY_API_KEY and not authorized(incoming):
        return False, None
    if UPSTREAM_API_KEY:
        return True, f"Bearer {UPSTREAM_API_KEY}"
    # No key configured on the proxy: pass the caller's own key through.
    return True, incoming


def ms_since(start: float, end: float | None = None) -> int:
    return int(((end if end is not None else time.perf_counter()) - start) * 1000)


def parse_json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def apply_usage(record: dict, usage: dict | None) -> None:
    usage = usage if isinstance(usage, dict) else {}
    record["prompt_tokens"] = usage.get("prompt_tokens")
    record["completion_tokens"] = usage.get("completion_tokens")
    record["total_tokens"] = usage.get("total_tokens")


def error_body(message: str, status: int, trace_id: str | None = None) -> JSONResponse:
    headers = {"x-agenteval-trace-id": trace_id} if trace_id else None
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": {"message": message, "type": "proxy_error"}})


def read_agenteval_headers(request: Request) -> tuple[dict, str | None]:
    """Returns (settings, error). Values are validated because they end up in the database."""
    case_ref = request.headers.get("x-agenteval-case-id")
    if case_ref is not None and not CASE_REF_PATTERN.match(case_ref):
        return {}, "X-AgentEval-Case-Id must be 1 to 64 characters of A-Z a-z 0-9 . _ : -"
    requested = {v.strip().lower() for v in (request.headers.get("x-agenteval-evaluations") or "").split(",") if v.strip()}
    unknown = requested - KNOWN_EVALUATIONS
    if unknown:
        return {}, f"Unknown X-AgentEval-Evaluations value: {', '.join(sorted(unknown))}"
    return {"case_ref": case_ref, "grounded_eval": "grounded" in requested}, None


async def submit(record: dict) -> None:
    """Hand a finished trace to the durable spool, off the event loop."""
    if REDACT_PII:
        total = 0
        for field in ("messages", "response", "tool_calls"):
            record[field], n = redact(record.get(field))
            total += n
        record["redaction_count"] = total
    outcome = record.get("error_type") or ("ok" if record.get("status_code") == 200 else f"http_{record.get('status_code')}")
    count("requests", outcome)
    log.info("trace recorded", extra={k: record.get(k) for k in (
        "trace_id", "status_code", "latency_ms", "model", "response_model", "error_type", "case_ref", "streamed")})
    await anyio.to_thread.run_sync(app.state.store.submit, record)


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    allowed, authorization = upstream_authorization(request.headers.get("authorization"))
    if not allowed:
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Missing or invalid proxy API key.", "type": "invalid_request_error"}},
        )
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        return error_body(f"Request body larger than {MAX_REQUEST_BYTES} bytes.", 413)
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        return error_body(f"Request body larger than {MAX_REQUEST_BYTES} bytes.", 413)
    settings, problem = read_agenteval_headers(request)
    if problem:
        return error_body(problem, 400)

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in DROP_REQUEST_HEADERS and not k.lower().startswith(AGENTEVAL_HEADER_PREFIX)}
    if authorization:
        headers["authorization"] = authorization

    # Only chat completions are recorded. Everything else is forwarded untouched.
    record = None
    hide_usage_chunk = False
    if request.method == "POST" and path == "chat/completions":
        payload = parse_json(body)
        if isinstance(payload, dict):
            streamed = payload.get("stream") is True
            record = {"trace_id": uuid.uuid4().hex, "created_at": time.time(), "model": payload.get("model"),
                      "messages": payload.get("messages"), "streamed": streamed, **settings}
            if streamed:
                # Ask upstream for token usage at the end of the stream. If the caller did not
                # ask for it, the extra chunk is filtered out before the caller sees it.
                options = payload.get("stream_options") or {}
                if not options.get("include_usage"):
                    hide_usage_chunk = True
                    payload["stream_options"] = {**options, "include_usage": True}
                    body = json.dumps(payload).encode()

    url = f"{UPSTREAM_BASE}/v1/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    start = time.perf_counter()
    upstream_request = app.state.http.build_request(request.method, url, headers=headers, content=body)
    try:
        upstream = await app.state.http.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        return await upstream_failed(exc, record, start)

    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}
    request_id = upstream.headers.get("x-request-id") or uuid.uuid4().hex
    if record is not None:
        response_headers["x-agenteval-trace-id"] = record["trace_id"]

    if record is not None and record["streamed"] and upstream.status_code == 200:
        record.update(status_code=200, request_id=request_id)
        return StreamingResponse(
            relay_stream(upstream, record, StreamAccumulator(hide_usage_chunk), start),
            status_code=200,
            headers=response_headers,
        )

    if record is None and upstream.headers.get("content-type", "").startswith("text/event-stream"):
        # Unrecorded streaming endpoints (for example the Responses API) are relayed as they arrive.
        return StreamingResponse(relay_raw(upstream), status_code=upstream.status_code, headers=response_headers)

    try:
        content = await upstream.aread()
    except httpx.HTTPError as exc:
        return await upstream_failed(exc, record, start)
    finally:
        await upstream.aclose()

    if record is not None:
        record.update(status_code=upstream.status_code, latency_ms=ms_since(start), request_id=request_id)
        data = parse_json(content)
        if isinstance(data, dict):
            record["response"] = data
            record["response_model"] = data.get("model") if isinstance(data.get("model"), str) else None
            apply_usage(record, data.get("usage"))
            choices = data.get("choices") or []
            if choices and isinstance(choices[0], dict):
                record["tool_calls"] = (choices[0].get("message") or {}).get("tool_calls")
        else:
            record["response"] = {"raw_body": content[:2000].decode("utf-8", "replace")}
        await submit(record)

    return Response(content=content, status_code=upstream.status_code, headers=response_headers)


def classify(exc: Exception) -> tuple[str, int]:
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout", 504
    if isinstance(exc, httpx.ConnectError):
        return "upstream_unreachable", 502
    return "upstream_error", 502


async def upstream_failed(exc: Exception, record: dict | None, start: float) -> JSONResponse:
    """Upstream could not be reached, timed out or dropped the connection."""
    error_type, status = classify(exc)
    count("upstream_errors", error_type)
    log.warning("upstream request failed: %s", type(exc).__name__,
                extra={"error_type": error_type, "trace_id": record and record["trace_id"]})
    if record is not None:
        record.update(status_code=status, latency_ms=ms_since(start), request_id=uuid.uuid4().hex,
                      error_type=error_type, error=f"{type(exc).__name__}: {exc}"[:500],
                      response={"proxy_error": f"{type(exc).__name__}: {exc}"[:500]})
        await submit(record)
    return error_body(f"Upstream request failed: {type(exc).__name__}", status,
                      record["trace_id"] if record else None)


async def relay_raw(upstream: httpx.Response):
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await upstream.aclose()


async def relay_stream(upstream: httpx.Response, record: dict, acc: StreamAccumulator, start: float):
    """Forward events as they arrive, then log the rebuilt completion."""
    upstream_error = None
    upstream_finished = False
    try:
        try:
            async for chunk in upstream.aiter_bytes():
                for event in acc.feed(chunk, time.perf_counter()):
                    yield event
            upstream_finished = True
        except httpx.HTTPError as exc:
            # Headers are already sent, so the status cannot change. Without an explicit error
            # event the caller would see a short but normal looking stream; the OpenAI SDKs
            # raise on an event that carries an "error" object.
            upstream_error = exc
            error_type, _ = classify(exc)
            count("upstream_errors", f"{error_type}_mid_stream")
            yield (b"data: " + json.dumps({"error": {
                "message": f"Upstream stream failed: {type(exc).__name__}", "type": "proxy_error"}}).encode() + b"\n\n")
            return
        rest = acc.flush()
        if rest:
            yield rest
    finally:
        # Runs on normal completion, on upstream failure and when the caller disconnects.
        if upstream_error is not None:
            record["error_type"] = classify(upstream_error)[0] + "_mid_stream"
            record["error"] = f"{type(upstream_error).__name__}: {upstream_error}"[:500]
        elif not acc.done:
            record["error_type"] = "upstream_incomplete_stream" if upstream_finished else "client_disconnect"
        record.update(
            response=acc.response(),
            response_model=acc.model,
            tool_calls=acc.tool_calls or None,
            latency_ms=ms_since(start),
            ttft_ms=ms_since(start, acc.first_token_at) if acc.first_token_at else None,
        )
        apply_usage(record, acc.usage)
        with anyio.CancelScope(shield=True):
            await submit(record)
            await upstream.aclose()
