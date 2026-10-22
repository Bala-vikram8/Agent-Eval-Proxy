"""AgentEval proxy.

Forwards OpenAI compatible requests to UPSTREAM_BASE unchanged and records every
POST /v1/chat/completions call, streamed or not, in Postgres. Recording happens after the
response is on its way to the caller and can never fail the request.
"""
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from store import TraceStore
from streaming import StreamAccumulator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    app.state.store = TraceStore(DATABASE_URL)
    log.info("forwarding to %s, proxy key %s", UPSTREAM_BASE, "required" if PROXY_API_KEY else "not required")
    yield
    await app.state.http.aclose()
    app.state.store.close()


app = FastAPI(title="AgentEval Proxy", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


def upstream_authorization(incoming: str | None) -> tuple[bool, str | None]:
    """Decide which Authorization header goes upstream. Returns (allowed, header)."""
    if PROXY_API_KEY and not hmac.compare_digest(incoming or "", f"Bearer {PROXY_API_KEY}"):
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
    usage = usage or {}
    record["prompt_tokens"] = usage.get("prompt_tokens")
    record["completion_tokens"] = usage.get("completion_tokens")
    record["total_tokens"] = usage.get("total_tokens")


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    allowed, authorization = upstream_authorization(request.headers.get("authorization"))
    if not allowed:
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Missing or invalid proxy API key.", "type": "invalid_request_error"}},
        )

    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS}
    if authorization:
        headers["authorization"] = authorization

    # Only chat completions are recorded. Everything else is forwarded untouched.
    record = None
    hide_usage_chunk = False
    if request.method == "POST" and path == "chat/completions":
        payload = parse_json(body)
        if isinstance(payload, dict):
            streamed = payload.get("stream") is True
            record = {"model": payload.get("model"), "messages": payload.get("messages"), "streamed": streamed}
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
        return upstream_failed(exc, record, start)

    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}
    request_id = upstream.headers.get("x-request-id") or uuid.uuid4().hex

    if record is not None and record["streamed"] and upstream.status_code == 200:
        record.update(status_code=200, request_id=request_id)
        return StreamingResponse(
            relay_stream(upstream, record, StreamAccumulator(hide_usage_chunk), start),
            status_code=200,
            headers=response_headers,
        )

    try:
        content = await upstream.aread()
    except httpx.HTTPError as exc:
        return upstream_failed(exc, record, start)
    finally:
        await upstream.aclose()

    if record is not None:
        record.update(status_code=upstream.status_code, latency_ms=ms_since(start), request_id=request_id)
        data = parse_json(content)
        if isinstance(data, dict):
            record["response"] = data
            apply_usage(record, data.get("usage"))
            choices = data.get("choices") or []
            if choices:
                record["tool_calls"] = (choices[0].get("message") or {}).get("tool_calls")
        else:
            record["response"] = {"raw_body": content[:2000].decode("utf-8", "replace")}
        app.state.store.submit(record)

    return Response(content=content, status_code=upstream.status_code, headers=response_headers)


def upstream_failed(exc: Exception, record: dict | None, start: float) -> JSONResponse:
    """Upstream could not be reached or dropped the connection: answer 502 and log it."""
    log.warning("upstream request failed: %r", exc)
    if record is not None:
        record.update(status_code=502, latency_ms=ms_since(start), request_id=uuid.uuid4().hex,
                      response={"proxy_error": f"{type(exc).__name__}: {exc}"})
        app.state.store.submit(record)
    return JSONResponse(
        status_code=502,
        content={"error": {"message": f"Upstream request failed: {type(exc).__name__}", "type": "proxy_error"}},
    )


async def relay_stream(upstream: httpx.Response, record: dict, acc: StreamAccumulator, start: float):
    """Forward events as they arrive, then log the rebuilt completion."""
    try:
        async for chunk in upstream.aiter_bytes():
            for event in acc.feed(chunk, time.perf_counter()):
                yield event
        rest = acc.flush()
        if rest:
            yield rest
    finally:
        # Runs on normal completion and when the caller disconnects mid stream.
        response = acc.response()
        record.update(
            response=response,
            tool_calls=acc.tool_calls or None,
            latency_ms=ms_since(start),
            ttft_ms=ms_since(start, acc.first_token_at) if acc.first_token_at else None,
        )
        apply_usage(record, acc.usage)
        app.state.store.submit(record)
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
