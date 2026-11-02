"""Authenticated, bounded OpenAI-compatible proxy with minimized durable traces.

The deployment scope is one organization. Application admission limits are per
process; a deployment gateway must enforce aggregate quotas across replicas.
"""
import json
import logging
import math
import os
import re
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from privacy import content_mode, custom_patterns, prepare_trace, redact_text
from security import Admission, matches_bearer, read_secret
from store import TraceStore
from streaming import StreamAccumulator, StreamLimitError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("agenteval.proxy")
logging.getLogger('httpx').setLevel(logging.WARNING)

DEFAULT_UPSTREAM = 'https://api.openai.com'
UPSTREAM_BASE = os.getenv('UPSTREAM_BASE', DEFAULT_UPSTREAM).rstrip('/')
UPSTREAM_API_KEY = read_secret('UPSTREAM_API_KEY') or (
    read_secret('OPENAI_API_KEY') if UPSTREAM_BASE == DEFAULT_UPSTREAM else None)
PROXY_API_KEY = read_secret('PROXY_API_KEY')
ALLOW_INSECURE_LOCAL = os.getenv('ALLOW_INSECURE_LOCAL', 'false').lower() == 'true'
DATABASE_URL = os.getenv('DATABASE_URL', '')
MAX_REQUEST_BYTES = int(os.getenv('PROXY_MAX_REQUEST_BYTES', '1048576'))
MAX_RESPONSE_BYTES = int(os.getenv('PROXY_MAX_RESPONSE_BYTES', '4194304'))
MAX_CONCURRENT = int(os.getenv('PROXY_MAX_CONCURRENT', '64'))
REQUESTS_PER_MINUTE = int(os.getenv('PROXY_REQUESTS_PER_MINUTE', '1200'))
MAX_STREAM_SECONDS = float(os.getenv('PROXY_MAX_STREAM_SECONDS', '300'))
MAX_REQUEST_SECONDS = float(os.getenv('PROXY_MAX_REQUEST_SECONDS', '30'))
MAX_EVENT_BYTES = int(os.getenv('PROXY_MAX_SSE_EVENT_BYTES', '262144'))
MAX_CAPTURE_BYTES = int(os.getenv('TRACE_MAX_CAPTURE_BYTES', '262144'))
SAFE_ID = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')
SAFE_MODEL = re.compile(r'[A-Za-z0-9/_.:-]{1,128}\Z')
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-connection', 'proxy-authorization',
               'proxy-authenticate', 'te', 'trailer', 'transfer-encoding', 'upgrade'}
DROP_REQUEST_HEADERS = HOP_HEADERS | {'host', 'content-length', 'accept-encoding',
    'authorization', 'cookie', 'x-api-key', 'x-proxy-api-key', 'x-agent-id'}
DROP_RESPONSE_HEADERS = HOP_HEADERS | {'content-length', 'content-encoding', 'authorization',
                                      'set-cookie', 'x-api-key'}
SAFE_ERROR_CODES = {'model_not_found', 'rate_limit_exceeded', 'insufficient_quota',
                    'invalid_api_key', 'context_length_exceeded'}


class SizeLimitError(ValueError):
    pass


def validate_configuration():
    parsed = urlsplit(UPSTREAM_BASE)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError('UPSTREAM_BASE must be an HTTP(S) URL without credentials, query, or fragment')
    if not ALLOW_INSECURE_LOCAL:
        if not PROXY_API_KEY or len(PROXY_API_KEY) < 32:
            raise ValueError('Set a strong PROXY_API_KEY or explicitly enable ALLOW_INSECURE_LOCAL for synthetic local use')
        if not UPSTREAM_API_KEY or UPSTREAM_API_KEY == PROXY_API_KEY:
            raise ValueError('Authenticated mode requires a distinct upstream credential')
        if parsed.scheme != 'https':
            raise ValueError('Authenticated deployment upstreams require HTTPS')
    limits = (MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_CONCURRENT, REQUESTS_PER_MINUTE,
              MAX_STREAM_SECONDS, MAX_REQUEST_SECONDS, MAX_EVENT_BYTES, MAX_CAPTURE_BYTES)
    if any(not math.isfinite(value) or value <= 0 for value in limits):
        raise ValueError('Resource limits must be positive')
    content_mode()
    custom_patterns()


@asynccontextmanager
async def lifespan(application: FastAPI):
    validate_configuration()
    application.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(MAX_STREAM_SECONDS, connect=10.0),
        limits=httpx.Limits(max_connections=MAX_CONCURRENT, max_keepalive_connections=min(MAX_CONCURRENT, 50)),
        follow_redirects=False)
    application.state.store = TraceStore(DATABASE_URL)
    application.state.admission = Admission(MAX_CONCURRENT, REQUESTS_PER_MINUTE)
    application.state.counters = Counter()
    log.info('proxy started; authentication=%s content_mode=%s', bool(PROXY_API_KEY), content_mode())
    try:
        yield
    finally:
        await application.state.http.aclose()
        await anyio.to_thread.run_sync(application.state.store.close)


app = FastAPI(title='AgentEval Proxy', lifespan=lifespan,
              docs_url='/docs' if ALLOW_INSECURE_LOCAL else None,
              redoc_url=None)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={'error': {
        'message': message, 'type': 'proxy_error', 'code': code}})


def upstream_authorization(incoming: str | None) -> tuple[bool, str | None]:
    if PROXY_API_KEY and not matches_bearer(incoming, PROXY_API_KEY):
        return False, None
    if not PROXY_API_KEY and not ALLOW_INSECURE_LOCAL:
        return False, None
    if UPSTREAM_API_KEY:
        return True, f'Bearer {UPSTREAM_API_KEY}'
    # A proxy credential is never a provider credential, including local mode.
    return True, None if PROXY_API_KEY else incoming


@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.get('/ready')
async def ready():
    try:
        stats = await anyio.to_thread.run_sync(app.state.store.stats)
        if stats.get('accepting') is not True:
            return JSONResponse(status_code=503, content={'status': 'unavailable'})
    except Exception:
        return JSONResponse(status_code=503, content={'status': 'unavailable'})
    return {'status': 'ready'}


@app.get('/metrics')
async def metrics(request: Request):
    if not upstream_authorization(request.headers.get('authorization'))[0]:
        return error(401, 'invalid_api_key', 'Missing or invalid proxy API key.')
    stats = {}
    try:
        if hasattr(app.state.store, 'stats'):
            stats = await anyio.to_thread.run_sync(app.state.store.stats)
    except Exception:
        stats = {'available': False}
    return {'requests': dict(app.state.counters), 'active_requests': app.state.admission.active,
            'ingestion': stats, 'quota_scope': 'process'}


def ms_since(start: float, end: float | None = None) -> int:
    return max(0, int(((end if end is not None else time.perf_counter()) - start) * 1000))


def parse_json(raw: bytes):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def apply_usage(record: dict, usage) -> None:
    usage = usage if isinstance(usage, dict) else {}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        value = usage.get(key)
        record[key] = value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**31 else None


def filtered_headers(headers, dropped: set[str]) -> dict:
    nominated = {v.strip().lower() for v in headers.get('connection', '').split(',')}
    return {k: v for k, v in headers.items() if k.lower() not in dropped | nominated}


async def capture(record: dict) -> bool:
    try:
        prepared = prepare_trace(record)
        accepted = await anyio.to_thread.run_sync(app.state.store.submit, prepared)
        if accepted is not True:
            app.state.counters['trace_rejected'] += 1
            return False
        app.state.counters['trace_accepted'] += 1
        return True
    except Exception as exc:
        log.error('trace submission failed; category=%s', type(exc).__name__)
        app.state.counters['trace_rejected'] += 1
        return False


async def read_body(request: Request) -> bytes:
    content_length = request.headers.get('content-length')
    if content_length is not None:
        try:
            length = int(content_length)
            if length < 0:
                raise ValueError
        except ValueError:
            raise ValueError('Invalid content length') from None
        if length > MAX_REQUEST_BYTES:
            raise SizeLimitError
    result = bytearray()
    async for chunk in request.stream():
        if len(result) + len(chunk) > MAX_REQUEST_BYTES:
            raise SizeLimitError
        result.extend(chunk)
    return bytes(result)


def request_payload(body: bytes):
    payload = parse_json(body)
    if not isinstance(payload, dict):
        raise ValueError('Chat completion requests must be JSON objects')
    if not isinstance(payload.get('model'), str) or not SAFE_MODEL.fullmatch(payload['model']):
        raise ValueError('Invalid model identifier')
    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages or any(
            not isinstance(m, dict) or m.get('role') not in {'system', 'developer', 'user', 'assistant', 'tool', 'function'}
            for m in messages):
        raise ValueError('Messages must be a nonempty list of message objects')
    if 'stream' in payload and not isinstance(payload['stream'], bool):
        raise ValueError('Stream must be a boolean')
    options = payload.get('stream_options')
    if options is not None and (not isinstance(options, dict) or
            ('include_usage' in options and not isinstance(options['include_usage'], bool))):
        raise ValueError('Invalid stream options')
    context = None
    metadata = payload.get('metadata')
    if isinstance(metadata, dict) and 'evaluation_context' in metadata:
        context = metadata['evaluation_context']
        if isinstance(context, str):
            if len(context.encode()) > 16384:
                raise ValueError('Evaluation context is too large')
            context = parse_json(context.encode())
        if not isinstance(context, dict) or len(json.dumps(context).encode()) > 16384:
            raise ValueError('Evaluation context must be a bounded JSON object')
        context = {k: v for k, v in context.items() if k != 'trusted'}
        # Proxy evaluation references/configuration are not model input.
        payload['metadata'] = {k: v for k, v in metadata.items() if k != 'evaluation_context'}
        if not payload['metadata']:
            payload.pop('metadata')
    return payload, context


class ManagedStream(StreamingResponse):
    def __init__(self, *args, release, upstream, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release
        self.upstream = upstream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.release()
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


@app.api_route('/v1/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
async def proxy(path: str, request: Request):
    allowed, authorization = upstream_authorization(request.headers.get('authorization'))
    if not allowed:
        return error(401, 'invalid_api_key', 'Missing or invalid proxy API key.')
    if not ALLOW_INSECURE_LOCAL and (request.method, path) not in {
            ('GET', 'models'), ('POST', 'chat/completions')}:
        return error(403, 'endpoint_not_allowed', 'This endpoint is not enabled.')
    agent_id = request.headers.get('x-agent-id', 'default')
    if not SAFE_ID.fullmatch(agent_id):
        return error(400, 'invalid_agent_id', 'Invalid agent identifier.')
    if request.headers.get('content-encoding', 'identity').lower() != 'identity':
        return error(415, 'unsupported_encoding', 'Compressed request bodies are not supported.')
    rejected = app.state.admission.acquire()
    if rejected:
        app.state.counters['request_rejected'] += 1
        return error(rejected, 'rate_limited' if rejected == 429 else 'overloaded', 'Request admission limit reached.')
    streamed = False
    released = False

    def release():
        nonlocal released
        if not released:
            app.state.admission.release()
            released = True
    try:
        with anyio.fail_after(MAX_STREAM_SECONDS):
            response, streamed = await forward(path, request, authorization, agent_id, release)
        return response
    except TimeoutError:
        app.state.counters['request_timeout'] += 1
        record = getattr(request.state, 'trace_record', None)
        if record is not None:
            record.update(status_code=504, failure_category='upstream_timeout',
                          latency_ms=ms_since(request.state.upstream_started),
                          response={'proxy_error': 'TimeoutError'})
            # The deadline has expired. Persist the safe timeout record under a
            # shield before releasing admission, using its stable event id.
            with anyio.CancelScope(shield=True):
                if not await capture(record):
                    return error(503, 'trace_unavailable', 'Trace delivery is unavailable; the provider request may have completed.')
        return error(504, 'request_timeout', 'The upstream operation exceeded its deadline.')
    finally:
        if not streamed:
            release()


async def forward(path: str, request: Request, authorization: str | None, agent_id: str, release):
    try:
        with anyio.fail_after(MAX_REQUEST_SECONDS):
            body = await read_body(request)
    except TimeoutError:
        return error(408, 'request_timeout', 'The request body exceeded its deadline.'), False
    except SizeLimitError:
        return error(413, 'request_too_large', 'Request exceeds the configured size limit.'), False
    except ValueError:
        return error(400, 'invalid_request', 'Invalid request framing.'), False
    headers = filtered_headers(request.headers, DROP_REQUEST_HEADERS)
    headers['accept-encoding'] = 'identity'
    if authorization:
        headers['authorization'] = authorization
    record = None
    hide_usage_chunk = False
    if request.method == 'POST' and path == 'chat/completions':
        try:
            payload, context = request_payload(body)
        except ValueError as exc:
            return error(400, 'invalid_request', str(exc)), False
        wants_stream = payload.get('stream') is True
        record = {'event_id': str(uuid.uuid4()), 'agent_id': agent_id, 'model': payload['model'],
                  'messages': payload['messages'], 'streamed': wants_stream, 'evaluation_context': context}
        if wants_stream:
            options = payload.get('stream_options') or {}
            if not options.get('include_usage'):
                hide_usage_chunk = True
                payload['stream_options'] = {**options, 'include_usage': True}
        body = json.dumps(payload).encode()
        # Reject known queue outages/full capacity before spending provider
        # resources. This is an observation, not an atomic reservation: another
        # request can fill the queue before this request's final publish.
        try:
            queue_state = await anyio.to_thread.run_sync(app.state.store.stats)
            accepting = queue_state.get('accepting') is True
        except Exception:
            accepting = False
        if not accepting:
            app.state.counters['trace_admission_rejected'] += 1
            return error(503, 'trace_unavailable', 'Trace delivery is unavailable; the request was not forwarded.'), False
    url = f'{UPSTREAM_BASE}/v1/{path}'
    if request.url.query:
        url = f'{url}?{request.url.query}'
    start = time.perf_counter()
    request.state.trace_record = record
    request.state.upstream_started = start
    app.state.counters['forwarded'] += 1
    try:
        upstream_request = app.state.http.build_request(request.method, url, headers=headers, content=body)
        upstream = await app.state.http.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        return await upstream_failed(exc, record, start), False
    # Avoid allocating an unbounded decompressed gzip/deflate response before a
    # size check. OpenAI-compatible JSON/SSE works with identity encoding.
    if upstream.headers.get('content-encoding', 'identity').lower() != 'identity':
        await upstream.aclose()
        return await upstream_failed(SizeLimitError(), record, start), False
    response_headers = {k: redact_text(v) for k, v in
                        filtered_headers(upstream.headers, DROP_RESPONSE_HEADERS).items()}
    request_id = upstream.headers.get('x-request-id', uuid.uuid4().hex)[:128]
    if record is not None:
        record.update(status_code=upstream.status_code, request_id=request_id)
        response_headers['x-trace-id'] = record['event_id']
    if record is not None and record['streamed'] and upstream.status_code == 200:
        response_headers['x-trace-delivery'] = 'deferred'
        return ManagedStream(
            relay_stream(upstream, record, StreamAccumulator(hide_usage_chunk, MAX_EVENT_BYTES, MAX_CAPTURE_BYTES), start),
            status_code=200, headers=response_headers, release=release, upstream=upstream), True
    try:
        content = bytearray()
        async for chunk in upstream.aiter_bytes():
            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                raise SizeLimitError
            content.extend(chunk)
        content = bytes(content)
    except (httpx.HTTPError, SizeLimitError) as exc:
        return await upstream_failed(exc, record, start), False
    finally:
        await upstream.aclose()
    data = parse_json(content)
    if upstream.status_code >= 400:
        candidate = data.get('error') if isinstance(data, dict) else None
        code = candidate.get('code') if isinstance(candidate, dict) else None
        safe_error = {'error': {'message': 'The model provider rejected the request.', 'type': 'upstream_error',
                                'code': code if isinstance(code, str) and code in SAFE_ERROR_CODES else 'upstream_error'}}
        response_headers = {k: v for k, v in response_headers.items() if
                            k.lower() in {'content-type', 'retry-after', 'x-request-id', 'x-trace-id'} or
                            k.lower().startswith('x-ratelimit-')}
        data = safe_error
        content = json.dumps(safe_error).encode()
        response_headers['content-type'] = 'application/json'
    if record is not None:
        record.update(latency_ms=ms_since(start), response=data if isinstance(data, dict) else None)
        if isinstance(data, dict):
            apply_usage(record, data.get('usage'))
            choices = data.get('choices')
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get('message')
                if isinstance(message, dict):
                    record['tool_calls'] = message.get('tool_calls')
        if not await capture(record):
            return error(503, 'trace_unavailable', 'Trace delivery is unavailable; the provider request may have completed.'), False
        response_headers['x-trace-delivery'] = 'durable'
    return Response(content=content, status_code=upstream.status_code, headers=response_headers), False


async def upstream_failed(exc: Exception, record: dict | None, start: float):
    category = type(exc).__name__
    log.warning('upstream request failed; category=%s', category)
    if record is not None:
        record.update(status_code=502, latency_ms=ms_since(start), request_id=uuid.uuid4().hex,
                      failure_category='response_limit' if isinstance(exc, SizeLimitError) else 'upstream_unavailable',
                      response={'proxy_error': category})
        if not await capture(record):
            return error(503, 'trace_unavailable', 'Trace delivery is unavailable; the provider request may have completed.')
    return error(502, 'upstream_unavailable', 'The model provider is unavailable.')


async def relay_stream(upstream: httpx.Response, record: dict, acc: StreamAccumulator, start: float):
    failure = None
    ended = False
    received = 0
    try:
        with anyio.fail_after(MAX_STREAM_SECONDS):
            async for chunk in upstream.aiter_bytes():
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise SizeLimitError
                for event in acc.feed(chunk, time.perf_counter()):
                    yield event
            rest = acc.flush()
            if rest:
                yield rest
            ended = True
    except (httpx.HTTPError, TimeoutError, SizeLimitError, StreamLimitError) as exc:
        failure = 'response_limit' if isinstance(exc, (SizeLimitError, StreamLimitError)) else 'upstream_stream_error'
        log.warning('stream interrupted; category=%s', type(exc).__name__)
    finally:
        if not acc.done and failure is None:
            failure = 'incomplete_stream' if ended else 'client_disconnect'
        if acc.capture_truncated:
            failure = failure or 'capture_limit'
        if acc.malformed:
            failure = failure or 'malformed_stream'
        record.update(response=acc.response(), tool_calls=acc.tool_calls or None,
                      latency_ms=ms_since(start), ttft_ms=ms_since(start, acc.first_token_at) if acc.first_token_at else None,
                      stream_completed=acc.done, failure_category=failure)
        if failure and failure != 'capture_limit':
            record['status_code'] = 499 if failure == 'client_disconnect' else 502
        apply_usage(record, acc.usage)
        # Headers/bytes may already be on the wire. A final submission failure is
        # visible in authenticated metrics; it cannot change the HTTP status.
        with anyio.CancelScope(shield=True):
            await capture(record)
            await upstream.aclose()
