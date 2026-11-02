"""Minimize traces before they reach a queue or database.

Metadata is the safe default. Redacted capture is an explicit diagnostic opt-in,
not a guarantee that arbitrary confidential prose can be recognized automatically.
"""
import json
import os
import re2 as re
from urllib.parse import unquote, urlsplit

from security import read_secret

METADATA = {
    'event_id', 'request_id', 'agent_id', 'model', 'status_code', 'streamed',
    'prompt_tokens', 'completion_tokens', 'total_tokens', 'latency_ms', 'ttft_ms',
    'failure_category', 'stream_completed', 'created_at', 'captured_at', 'content_truncated',
}
CONTENT = {'messages', 'response', 'tool_calls', 'evaluation_context'}
SENSITIVE_KEY = re.compile(
    r'(?i)password|passwd|secret|token|authorization|api.?key|cookie|confidential|'
    r'patient|customer.?name|email|phone|ssn|credit.?card')
PATTERNS = (
    re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'),
    re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    re.compile(r'\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b'),
    re.compile(r'\b(?:\d[ -]?){12,18}\d\b'),
    re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b'),
    re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'),
    re.compile(r'(?i)\b(?:bearer\s+|(?:password|passwd|api[_ -]?key|secret|access[_ -]?token)\s*[:=]\s*)[^\s,;"\]}]+'),
    re.compile(r'(?im)\bconfidential\s*:\s*[^\r\n]+'),
)


def content_mode() -> str:
    mode = os.getenv('TRACE_CONTENT_MODE', 'metadata')
    if mode not in {'metadata', 'redacted'}:
        raise ValueError('TRACE_CONTENT_MODE must be metadata or redacted')
    return mode


def custom_patterns() -> list:
    values = json.loads(os.getenv('TRACE_REDACT_PATTERNS', '[]'))
    if not isinstance(values, list) or len(values) > 20 or any(
            not isinstance(p, str) or not 1 <= len(p) <= 256 for p in values):
        raise ValueError('TRACE_REDACT_PATTERNS must contain at most 20 bounded patterns')
    try:
        # RE2 guarantees linear-time execution. Unsupported constructs, such as
        # backreferences/lookarounds, fail configuration instead of risking a
        # catastrophic-backtracking pause on untrusted message text.
        options = re.Options()
        options.log_errors = False  # invalid patterns may themselves contain private text
        return [re.compile(p, options=options) for p in values]
    except re.error:
        raise ValueError('TRACE_REDACT_PATTERNS must use supported RE2 syntax') from None


def _known_secrets() -> list[str]:
    values = [read_secret(k) or '' for k in (
        'OPENAI_API_KEY', 'UPSTREAM_API_KEY', 'PROXY_API_KEY', 'JUDGE_API_KEY', 'DASHBOARD_PASSWORD')]
    try:
        password = urlsplit(os.getenv('DATABASE_URL', '')).password or ''
        values.extend((password, unquote(password)))
    except ValueError:
        pass
    return [value for value in values if len(value) >= 4]


def _capture_limit() -> int:
    # Leave room for typed metadata and JSON field names in the durable event.
    # A content budget larger than the transport limit must not turn an otherwise
    # valid model response into a predictable oversized-event rejection.
    return max(0, min(int(os.getenv('TRACE_MAX_CAPTURE_BYTES', '262144')),
                      int(os.getenv('TRACE_MAX_EVENT_BYTES', str(240 * 1024))) - 4096))


def redact_text(value: str) -> str:
    limit = max(256, _capture_limit())
    try:
        if len(value.encode()) > limit:
            return '[OMITTED]'
    except UnicodeError:
        return '[OMITTED]'
    for secret in _known_secrets():
        value = value.replace(secret, '[REDACTED]')
        if len(value.encode()) > limit:
            return '[OMITTED]'
    for pattern in (*PATTERNS, *custom_patterns()):
        value = pattern.sub('[REDACTED]', value)
        if len(value.encode()) > limit:
            return '[OMITTED]'
    return value


def _redact(value, depth: int = 0):
    if depth > 20:
        return '[OMITTED]'
    if isinstance(value, str):
        # Tool arguments often contain serialized JSON, whose sensitive keys must
        # receive the same treatment as a normal object.
        if value.startswith(('{', '[')):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    return json.dumps(_redact(parsed, depth + 1), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        return redact_text(value)
    if isinstance(value, dict):
        return {redact_text(str(k)): '[REDACTED]' if SENSITIVE_KEY.search(str(k)) else _redact(v, depth + 1)
                for k, v in list(value.items())[:1000]}
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value[:1000]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return '[OMITTED]'


def prepare_trace(record: dict) -> dict:
    """Return a fresh minimized record; repeated calls never restore omitted data."""
    mode = content_mode()
    result = {name: record[name] for name in METADATA if name in record}
    for name in ('model', 'request_id', 'agent_id'):
        if isinstance(result.get(name), str):
            result[name] = redact_text(result[name])[:128]
    result['content_mode'] = mode
    for name in CONTENT:
        result[name] = None
    if mode == 'redacted':
        limit = _capture_limit()
        # Bound work before running administrator-provided regular expressions.
        selected = {key: record.get(key) for key in CONTENT}
        try:
            raw = json.dumps(selected, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raw = ''
        if raw and len(raw.encode()) <= limit:
            redacted = {key: _redact(value) for key, value in selected.items()}
            if len(json.dumps(redacted, ensure_ascii=True, allow_nan=False).encode()) <= limit:
                result.update(redacted)
            else:
                result['content_truncated'] = True
        else:
            result['content_truncated'] = True
    return result
