"""Synthetic security and privacy regressions; no provider credentials or cloud resources."""
import asyncio
import copy
import json
import os
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from conftest import ROOT
sys.path.insert(0, str(ROOT / 'proxy'))
import main
from privacy import custom_patterns, prepare_trace, redact_text
from security import Admission, read_secret


def test_secure_startup_fails_closed(monkeypatch):
    monkeypatch.setattr(main, 'ALLOW_INSECURE_LOCAL', False)
    monkeypatch.setattr(main, 'PROXY_API_KEY', None)
    with pytest.raises(ValueError, match='PROXY_API_KEY'):
        main.validate_configuration()


def test_secure_startup_requires_distinct_key_and_tls(monkeypatch):
    monkeypatch.setattr(main, 'ALLOW_INSECURE_LOCAL', False)
    monkeypatch.setattr(main, 'PROXY_API_KEY', 'p' * 32)
    monkeypatch.setattr(main, 'UPSTREAM_API_KEY', 'p' * 32)
    monkeypatch.setattr(main, 'UPSTREAM_BASE', 'https://provider.example')
    with pytest.raises(ValueError, match='distinct'):
        main.validate_configuration()
    monkeypatch.setattr(main, 'UPSTREAM_API_KEY', 'u' * 32)
    monkeypatch.setattr(main, 'UPSTREAM_BASE', 'http://provider.example')
    with pytest.raises(ValueError, match='HTTPS'):
        main.validate_configuration()
    monkeypatch.setattr(main, 'UPSTREAM_BASE', 'https://user:password@provider.example')
    with pytest.raises(ValueError, match='credentials'):
        main.validate_configuration()


def test_proxy_key_never_becomes_upstream_key(monkeypatch):
    monkeypatch.setattr(main, 'PROXY_API_KEY', 'proxy-only-secret')
    monkeypatch.setattr(main, 'UPSTREAM_API_KEY', None)
    monkeypatch.setattr(main, 'ALLOW_INSECURE_LOCAL', True)
    assert main.upstream_authorization('Bearer proxy-only-secret') == (True, None)
    assert main.upstream_authorization('Bearer wrong') == (False, None)
    assert main.upstream_authorization('Bearer ' + chr(255)) == (False, None)
    monkeypatch.setattr(main, 'UPSTREAM_API_KEY', 'provider-only-secret')
    assert main.upstream_authorization('Bearer proxy-only-secret') == (True, 'Bearer provider-only-secret')


def test_caller_credentials_pass_only_in_explicit_local_passthrough(monkeypatch):
    monkeypatch.setattr(main, 'PROXY_API_KEY', None)
    monkeypatch.setattr(main, 'UPSTREAM_API_KEY', None)
    monkeypatch.setattr(main, 'ALLOW_INSECURE_LOCAL', False)
    assert main.upstream_authorization('Bearer caller') == (False, None)
    monkeypatch.setattr(main, 'ALLOW_INSECURE_LOCAL', True)
    assert main.upstream_authorization('Bearer caller') == (True, 'Bearer caller')


def test_process_admission_bounds_concurrency_and_rate():
    limit = Admission(concurrent=1, per_minute=1)
    assert limit.acquire() is None
    assert limit.acquire() == 503
    limit.release()
    assert limit.acquire() == 429
    assert limit.active == 0


def private_record():
    return {'event_id': '001', 'model': 'synthetic-model', 'agent_id': 'synthetic', 'status_code': 200,
            'messages': [{'role': 'user', 'content': 'Email alice@example.org; SSN 123-45-6789; phone 312-555-1212; '
                                                     'password=synthetic-pass; CONFIDENTIAL: future acquisition'}],
            'response': {'api_key': 'credential-should-disappear', 'text': 'Contact alice@example.org',
                         'business': {'confidential_plan': 'buy rival company'}},
            'tool_calls': [{'function': {'name': 'lookup', 'arguments': '{"email":"alice@example.org","password":"secret-value"}'}}],
            'evaluation_context': {'evidence': 'alice@example.org', 'reference': 'safe synthetic sentence'}}


def test_metadata_default_never_persists_content(monkeypatch):
    monkeypatch.delenv('TRACE_CONTENT_MODE', raising=False)
    record = private_record()
    prepared = prepare_trace(record)
    assert prepared['content_mode'] == 'metadata'
    assert prepared['messages'] is prepared['response'] is prepared['tool_calls'] is prepared['evaluation_context'] is None
    assert 'alice' not in json.dumps(prepared)
    assert prepared['event_id'] == record['event_id']


def test_redaction_synthetic_personal_credentials_and_confidential_fields(monkeypatch):
    monkeypatch.setenv('TRACE_CONTENT_MODE', 'redacted')
    record = private_record()
    original = copy.deepcopy(record)
    prepared = prepare_trace(record)
    serialized = json.dumps(prepared)
    for value in ['alice@example.org', '123-45-6789', '312-555-1212', 'synthetic-pass',
                  'credential-should-disappear', 'future acquisition', 'buy rival company', 'secret-value']:
        assert value not in serialized
    assert '[REDACTED]' in serialized
    assert record == original, 'redaction must not modify the request forwarded to the provider'
    assert prepare_trace(prepared) == prepared


def test_custom_patterns_known_keys_and_capture_budget(monkeypatch):
    monkeypatch.setenv('TRACE_CONTENT_MODE', 'redacted')
    monkeypatch.setenv('TRACE_REDACT_PATTERNS', json.dumps([r'PROJECT-\d+']))
    monkeypatch.setenv('UPSTREAM_API_KEY', 'synthetic-known-provider-key')
    record = {'messages': [{'content': 'PROJECT-4471 synthetic-known-provider-key'}]}
    text = json.dumps(prepare_trace(record))
    assert 'PROJECT-4471' not in text and 'synthetic-known-provider-key' not in text
    monkeypatch.setenv('TRACE_MAX_CAPTURE_BYTES', '20')
    result = prepare_trace(record)
    assert result['messages'] is None and result['content_truncated']
    monkeypatch.setenv('TRACE_CONTENT_MODE', 'raw')
    with pytest.raises(ValueError):
        prepare_trace(record)


def test_request_payload_shapes_and_reference_trust_boundary():
    for bad in [None, [], {'model': 'm', 'messages': [], 'stream': True},
                {'model': 'm', 'messages': [{'role': 'user'}], 'stream_options': ['bad']}]:
        with pytest.raises(ValueError):
            main.request_payload(json.dumps(bad).encode())
    original = {'model': 'm', 'messages': [{'role': 'user', 'content': 'synthetic'}],
                'metadata': {'evaluation_context': json.dumps({'trusted': True, 'reference_id': 'catalog-id'})}}
    payload, context = main.request_payload(json.dumps(original).encode())
    assert context == {'reference_id': 'catalog-id'}
    assert 'metadata' not in payload


def test_connection_nominated_and_credential_headers_are_removed():
    incoming = httpx.Headers({'Connection': 'X-Hop', 'X-Hop': 'private', 'Authorization': 'Bearer key',
                              'Cookie': 'session=secret', 'Content-Type': 'application/json'})
    assert main.filtered_headers(incoming, main.DROP_REQUEST_HEADERS) == {'content-type': 'application/json'}


def request(body: bytes, headers=(), method='POST', path='/v1/chat/completions'):
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {'type': 'http.disconnect'}
        sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return Request({'type': 'http', 'method': method, 'path': path, 'raw_path': path.encode(),
                    'query_string': b'', 'scheme': 'http', 'server': ('test', 80),
                    'headers': list(headers)}, receive)


def test_chunked_request_body_budget(monkeypatch):
    monkeypatch.setattr(main, 'MAX_REQUEST_BYTES', 4)
    with pytest.raises(main.SizeLimitError):
        asyncio.run(main.read_body(request(b'12345')))


def run_forward(monkeypatch, upstream_status=200, upstream_body=None, accepted=True, request_body=None):
    captured = []
    class Sink:
        def stats(self):
            return {'accepting': True}
        def submit(self, record):
            captured.append(record)
            return accepted
    def provider(req):
        return httpx.Response(upstream_status, json=upstream_body or {'choices': [{'message': {'content': 'alice@example.org'}}]}, request=req)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            monkeypatch.setattr(main.app.state, 'http', client, raising=False)
            monkeypatch.setattr(main.app.state, 'store', Sink(), raising=False)
            monkeypatch.setattr(main.app.state, 'admission', Admission(2, 100), raising=False)
            monkeypatch.setattr(main.app.state, 'counters', __import__('collections').Counter(), raising=False)
            monkeypatch.setattr(main, 'ALLOW_INSECURE_LOCAL', True)
            monkeypatch.setattr(main, 'PROXY_API_KEY', None)
            return await main.proxy('chat/completions', request(json.dumps(request_body or {
                'model': 'm', 'messages': [{'role': 'user', 'content': 'alice@example.org'}]}).encode()))
    return asyncio.run(scenario()), captured


def test_response_unchanged_but_capture_redacted(monkeypatch):
    monkeypatch.setenv('TRACE_CONTENT_MODE', 'redacted')
    response, captured = run_forward(monkeypatch)
    assert response.status_code == 200 and b'alice@example.org' in response.body
    assert 'alice@example.org' not in json.dumps(captured)
    assert response.headers['x-trace-delivery'] == 'durable'


def test_upstream_error_messages_and_malformed_codes_are_safe(monkeypatch):
    monkeypatch.setenv('TRACE_CONTENT_MODE', 'redacted')
    response, captured = run_forward(monkeypatch, 404, {'error': {'code': 'model_not_found', 'message': 'private credential alice@example.org'}})
    assert response.status_code == 404
    assert json.loads(response.body)['error']['code'] == 'model_not_found'
    assert b'alice@example.org' not in response.body
    response, _ = run_forward(monkeypatch, 500, {'error': {'code': ['malformed'], 'message': 'private'}})
    assert response.status_code == 500
    assert json.loads(response.body)['error']['code'] == 'upstream_error'


def test_durable_capture_failure_is_visible_to_caller(monkeypatch):
    response, _ = run_forward(monkeypatch, accepted=False)
    assert response.status_code == 503
    assert json.loads(response.body)['error']['code'] == 'trace_unavailable'
    unacknowledged, _ = run_forward(monkeypatch, accepted=None)
    assert unacknowledged.status_code == 503, 'Missing acknowledgement cannot claim durable delivery'


def test_oversized_upstream_response_is_bounded(monkeypatch):
    monkeypatch.setattr(main, 'MAX_RESPONSE_BYTES', 10)
    response, captured = run_forward(monkeypatch)
    assert response.status_code == 502
    assert captured[0]['failure_category'] == 'response_limit'


def test_configured_secret_files_are_loaded_and_redacted(monkeypatch, tmp_path):
    source = tmp_path / 'provider-key'
    source.write_text('synthetic-provider-file-key\n')
    source.chmod(0o600)
    monkeypatch.delenv('UPSTREAM_API_KEY', raising=False)
    monkeypatch.setenv('UPSTREAM_API_KEY_FILE', str(source))
    assert read_secret('UPSTREAM_API_KEY') == 'synthetic-provider-file-key'
    assert 'synthetic-provider-file-key' not in redact_text('Provider synthetic-provider-file-key')
    monkeypatch.setenv('UPSTREAM_API_KEY', 'another-synthetic-key')
    with pytest.raises(ValueError, match='either'):
        read_secret('UPSTREAM_API_KEY')
    monkeypatch.delenv('UPSTREAM_API_KEY')
    source.write_bytes(b'x' * 16385)
    with pytest.raises(ValueError, match='16 KiB'):
        read_secret('UPSTREAM_API_KEY')


def test_unsupported_redaction_patterns_fail_without_logging_pattern(monkeypatch, capfd):
    monkeypatch.setenv('TRACE_REDACT_PATTERNS', json.dumps([r'(private-secret)\1']))
    with pytest.raises(ValueError, match='RE2 syntax'):
        custom_patterns()
    assert 'private-secret' not in capfd.readouterr().err


def test_redaction_uses_linear_time_patterns_for_adversarial_text():
    # Enforce a process deadline: replacing RE2 with a backtracking engine must
    # fail this test, rather than hang the entire test runner indefinitely.
    code = "from privacy import redact_text; assert redact_text('a'*200000+'!').endswith('!')"
    started = time.monotonic()
    completed = subprocess.run([sys.executable, '-c', code], cwd=ROOT / 'proxy',
        env={**os.environ, 'TRACE_REDACT_PATTERNS': json.dumps([r'^(a+)+$'])},
        capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert time.monotonic() - started < 5


def test_redaction_replacement_expansion_and_unicode_are_bounded(monkeypatch):
    monkeypatch.setenv('TRACE_CONTENT_MODE', 'redacted')
    monkeypatch.setenv('TRACE_REDACT_PATTERNS', json.dumps(['.'] * 20))
    prepared = prepare_trace({'messages': [{'content': 'x' * 1000}]})
    assert len(json.dumps(prepared).encode()) < 240 * 1024
    assert 'x' * 1000 not in json.dumps(prepared)
    monkeypatch.setenv('TRACE_REDACT_PATTERNS', '[]')
    invalid_unicode = prepare_trace({'messages': [{'content': '\ud800'}]})
    assert invalid_unicode['messages'][0]['content'] == '[OMITTED]'


def test_slow_request_body_deadline_releases_admission(monkeypatch):
    async def slow_receive():
        await asyncio.sleep(.2)
        return {'type': 'http.request', 'body': b'{}', 'more_body': False}
    body = request(b'{}')
    body._receive = slow_receive
    monkeypatch.setattr(main, 'ALLOW_INSECURE_LOCAL', True)
    monkeypatch.setattr(main, 'PROXY_API_KEY', None)
    monkeypatch.setattr(main, 'MAX_REQUEST_SECONDS', .01)
    monkeypatch.setattr(main.app.state, 'admission', Admission(1, 100), raising=False)
    monkeypatch.setattr(main.app.state, 'counters', __import__('collections').Counter(), raising=False)
    result = asyncio.run(main.proxy('chat/completions', body))
    assert result.status_code == 408
    assert main.app.state.admission.active == 0


def test_total_upstream_deadline_closes_response_and_captures_failure(monkeypatch):
    captured = []
    class SlowBody(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(.01)
                yield b' '
        async def aclose(self):
            self.closed = True
    stream = SlowBody()
    class Sink:
        def stats(self):
            return {'accepting': True}
        def submit(self, record):
            captured.append(record)
            return True
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, stream=stream, request=req))) as client:
            monkeypatch.setattr(main.app.state, 'http', client, raising=False)
            monkeypatch.setattr(main.app.state, 'store', Sink(), raising=False)
            monkeypatch.setattr(main.app.state, 'admission', Admission(1, 100), raising=False)
            monkeypatch.setattr(main.app.state, 'counters', __import__('collections').Counter(), raising=False)
            monkeypatch.setattr(main, 'ALLOW_INSECURE_LOCAL', True)
            monkeypatch.setattr(main, 'PROXY_API_KEY', None)
            monkeypatch.setattr(main, 'MAX_STREAM_SECONDS', .03)
            return await main.proxy('chat/completions', request(json.dumps({
                'model': 'm', 'messages': [{'role': 'user', 'content': 'synthetic'}]}).encode()))
    response = asyncio.run(scenario())
    assert response.status_code == 504
    assert captured[0]['status_code'] == 504
    assert captured[0]['failure_category'] == 'upstream_timeout'
    assert stream.closed and main.app.state.admission.active == 0


def test_numeric_usage_larger_than_postgres_integer_is_omitted():
    record = {}
    main.apply_usage(record, {'prompt_tokens': 2**40, 'completion_tokens': True, 'total_tokens': 100})
    assert record == {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': 100}


def test_metadata_only_http_capture_never_persists_message(start_proxy, database, mock_upstream, db):
    from conftest import wait_for_traces
    url = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=mock_upstream,
                      UPSTREAM_API_KEY='synthetic-provider-key', TRACE_CONTENT_MODE='metadata')
    reply = httpx.post(url + '/v1/chat/completions', json={'model': 'gpt-4o-mini',
        'messages': [{'role': 'user', 'content': 'Synthetic email alice@example.org asks about login.'}]})
    assert reply.status_code == 200
    assert reply.headers['x-trace-delivery'] == 'durable'
    [stored] = wait_for_traces(db, 1)
    assert stored['messages'] is stored['response'] is stored['tool_calls'] is None
    assert stored['content_mode'] == 'metadata' and stored['total_tokens'] > 0
    assert 'alice@example.org' not in json.dumps(stored, default=str)


def test_full_queue_rejects_http_request_before_provider_call(start_proxy, database, mock_upstream, db):
    provider_key = 'synthetic-preflight-' + uuid.uuid4().hex
    url = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=mock_upstream,
                      UPSTREAM_API_KEY=provider_key, TRACE_INGEST_EMBEDDED='false', TRACE_QUEUE_CAPACITY='1')
    body = {'model': 'gpt-4o-mini', 'messages': [{'role': 'user', 'content': 'Synthetic login ticket'}]}
    first = httpx.post(url + '/v1/chat/completions', json=body)
    assert first.status_code == 200 and first.headers['x-trace-delivery'] == 'durable'
    second = httpx.post(url + '/v1/chat/completions', json=body)
    assert second.status_code == 503 and second.json()['error']['code'] == 'trace_unavailable'
    seen = httpx.get(mock_upstream + '/mock/seen-authorization').json()['authorization']
    assert seen.count('Bearer ' + provider_key) == 1, 'Full queue must not cause another provider call'
    metrics = httpx.get(url + '/metrics').json()
    assert metrics['requests']['trace_admission_rejected'] == 1
    assert metrics['requests']['forwarded'] == 1
