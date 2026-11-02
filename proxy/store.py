"""Durable acceptance and idempotent pooled Postgres consumption.

submit acknowledges transport persistence, not insertion. SQLite survives process
restart on retained storage; SQS provides replicated cloud durability. Neither
makes upstream model execution and trace capture atomic.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool

from privacy import prepare_trace
from queueing import SQSQueue, SQLiteQueue

log = logging.getLogger('agenteval.ingestion')
FIELDS = (
    'event_id', 'request_id', 'agent_id', 'model', 'messages', 'response', 'status_code', 'streamed',
    'prompt_tokens', 'completion_tokens', 'total_tokens', 'latency_ms', 'ttft_ms', 'tool_calls',
    'evaluation_context', 'content_mode', 'failure_category', 'stream_completed', 'captured_at',
)
JSON_FIELDS = {'messages', 'response', 'tool_calls', 'evaluation_context'}
INSERT_SQL = '''
INSERT INTO traces (
    created_at, event_id, request_id, agent_id, model, messages, response, status_code, streamed,
    prompt_tokens, completion_tokens, total_tokens, latency_ms, ttft_ms, tool_calls,
    evaluation_context, content_mode, failure_category, stream_completed
) VALUES (
    coalesce(to_timestamp(%(captured_at)s),now()), %(event_id)s, %(request_id)s, %(agent_id)s, %(model)s, %(messages)s, %(response)s,
    %(status_code)s, %(streamed)s, %(prompt_tokens)s, %(completion_tokens)s, %(total_tokens)s,
    %(latency_ms)s, %(ttft_ms)s, %(tool_calls)s, %(evaluation_context)s, %(content_mode)s,
    %(failure_category)s, %(stream_completed)s
)
ON CONFLICT (event_id) WHERE event_id IS NOT NULL DO NOTHING
'''


class InvalidTrace(ValueError):
    pass


def valid_capture_timestamp(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= time.time() + 60
    except OverflowError:
        return False


def minimized_record(record: dict) -> dict:
    clean = prepare_trace(record)
    # A consumer can further minimize a producer's trace, but cannot promote a
    # metadata-only event to content capture merely by changing its own config.
    if record.get('content_mode') == 'metadata':
        clean['content_mode'] = 'metadata'
        for name in JSON_FIELDS:
            clean[name] = None
    return clean


class TraceStore:
    def __init__(self, dsn: str, workers: int = 4, *, queue=None, embedded: bool | None = None):
        self._dsn = dsn
        self._workers = max(1, min(workers, 16))
        self._pool: ThreadedConnectionPool | None = None
        self._lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._counters = {'publish_errors': 0, 'database_errors': 0, 'consumer_errors': 0,
                          'rejected_after_close': 0, 'queue_errors': 0,
                          'queue_busy_errors': 0, 'queue_locked_errors': 0}
        self._closed = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._retry_base = float(os.getenv('TRACE_RETRY_BASE_SECONDS', '1'))
        self._shutdown_seconds = float(os.getenv('TRACE_SHUTDOWN_SECONDS', '5'))
        if not math.isfinite(self._retry_base) or not 0 <= self._retry_base <= 300:
            raise ValueError('Trace retry base must be a finite duration from zero to 300 seconds')
        if not math.isfinite(self._shutdown_seconds) or not 0 <= self._shutdown_seconds <= 30:
            raise ValueError('Trace shutdown must be a finite duration from zero to 30 seconds')
        retention_age = float(os.getenv('TRACE_RETENTION_DAYS', '7')) * 86400
        event_age = float(os.getenv('TRACE_EVENT_MAX_AGE_SECONDS', '604800'))
        if not all(math.isfinite(value) and value >= 1 for value in (retention_age, event_age)):
            raise ValueError('Trace retention limits must be positive finite durations')
        self._max_event_age = min(retention_age, event_age)
        self._threads: list[threading.Thread] = []
        self.queue = queue if queue is not None else self._make_queue(dsn)
        if embedded is None:
            embedded = os.getenv('TRACE_INGEST_EMBEDDED', 'true').lower() == 'true'
        if embedded:
            for index in range(self._workers):
                thread = threading.Thread(target=self._work, name=f'trace-ingest-{index}', daemon=True)
                thread.start()
                self._threads.append(thread)

    @staticmethod
    def _make_queue(dsn: str):
        backend = os.getenv('TRACE_QUEUE_BACKEND', 'sqlite')
        max_size = int(os.getenv('TRACE_MAX_EVENT_BYTES', str(240 * 1024)))
        lease = max(15, int(os.getenv('TRACE_INGEST_LEASE_SECONDS', '30')))
        if backend == 'sqs':
            return SQSQueue(os.environ['TRACE_SQS_QUEUE_URL'], max_message_bytes=max_size, lease_seconds=lease,
                            capacity=int(os.getenv('TRACE_QUEUE_CAPACITY', '10000')),
                            wait_seconds=int(os.getenv('TRACE_SQS_WAIT_SECONDS', '20')))
        if backend != 'sqlite':
            raise ValueError('Unsupported TRACE_QUEUE_BACKEND')
        # Never expose DSN credentials in a filename or mix unrelated test DBs.
        fingerprint = hashlib.sha256(dsn.encode()).hexdigest()[:20]
        default = Path('/tmp') / f'agenteval-{os.getuid()}' / f'{fingerprint}.sqlite3'
        return SQLiteQueue(os.getenv('TRACE_SPOOL_PATH', str(default)),
            capacity=int(os.getenv('TRACE_QUEUE_CAPACITY', '10000')),
            max_bytes=int(os.getenv('TRACE_QUEUE_MAX_BYTES', str(256 * 1024 * 1024))),
            max_message_bytes=max_size, lease_seconds=lease,
            max_attempts=int(os.getenv('TRACE_INGEST_MAX_ATTEMPTS', '8')))

    def _count(self, key):
        with self._counter_lock:
            self._counters[key] += 1

    def _queue_error(self, exc: Exception) -> str:
        """Classify backend failures without exposing SQL, paths or payloads."""
        self._count('queue_errors')
        code = getattr(exc, 'sqlite_errorcode', None)
        # Extended SQLite result codes preserve the primary code in the low
        # byte (for example SQLITE_BUSY_SNAPSHOT). No exception text is read.
        primary = (code & 0xff) if isinstance(exc, sqlite3.Error) and type(code) is int else None
        if primary == sqlite3.SQLITE_BUSY:
            self._count('queue_busy_errors')
            return 'sqlite_busy'
        if primary == sqlite3.SQLITE_LOCKED:
            self._count('queue_locked_errors')
            return 'sqlite_locked'
        return 'backend_unavailable'

    def submit(self, record: dict) -> bool:
        """Synchronous durable accept; ASGI must invoke this in a thread.

        False means rejection: callers must not claim trace capture succeeded.
        """
        if self._closed:
            self._count('rejected_after_close')
            return False
        try:
            clean = minimized_record(record)
            clean['event_id'] = clean.get('event_id') or uuid.uuid4().hex
            clean['agent_id'] = clean.get('agent_id') or 'default'
            clean.setdefault('captured_at', time.time())
            accepted = self.queue.enqueue(clean)
        except Exception as exc:
            self._count('publish_errors')
            category = self._queue_error(exc) if isinstance(exc, sqlite3.Error) else type(exc).__name__
            log.warning('trace_publish_failed category=%s', category)
            return False
        if accepted:
            self._wake.set()
        return bool(accepted)

    def stats(self) -> dict:
        try:
            result = self.queue.stats()
        except Exception as exc:
            result = {'backend': os.getenv('TRACE_QUEUE_BACKEND', 'sqlite'), 'accepting': False,
                      'queue_unavailable': True, 'queue_error_category': self._queue_error(exc)}
        with self._counter_lock:
            return {**result, **self._counters, 'accepting': bool(result.get('accepting')) and not self._closed}

    def _connection_pool(self) -> ThreadedConnectionPool:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadedConnectionPool(1, self._workers + 1, self._dsn,
                    connect_timeout=3, options='-c statement_timeout=5000 -c lock_timeout=2000')
            return self._pool

    def _insert(self, record: dict):
        if not isinstance(record, dict) or not isinstance(record.get('event_id'), str):
            raise InvalidTrace('invalid_trace')
        if len(record['event_id']) > 128 or not record['event_id']:
            raise InvalidTrace('invalid_event_id')
        captured = record.get('captured_at')
        if not valid_capture_timestamp(captured):
            raise InvalidTrace('invalid_capture_timestamp')
        clean = minimized_record(record)
        clean['agent_id'] = clean.get('agent_id') or 'default'
        params = {}
        for field in FIELDS:
            value = clean.get(field)
            params[field] = Json(value) if field in JSON_FIELDS and value is not None else value
        pool = self._connection_pool()
        conn = pool.getconn()
        try:
            with conn, conn.cursor() as cur:
                # Serialize deletion and insertion for this event, without
                # holding any transaction during provider/queue API calls.
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                            ('agenteval_trace:' + record['event_id'],))
                cur.execute('SELECT 1 FROM trace_tombstones WHERE event_id=%s', (record['event_id'],))
                if cur.fetchone() is not None:
                    return 'erased'
                cur.execute(INSERT_SQL, params)
                return 'delivered'
        finally:
            pool.putconn(conn, close=bool(conn.closed))

    def process_one(self) -> bool:
        """ACK only after commit; replay after an ACK crash is idempotent."""
        delivery = self.queue.claim()
        if delivery is None:
            return False
        captured = delivery.record.get('captured_at') if isinstance(delivery.record, dict) else None
        if valid_capture_timestamp(captured) and captured < time.time() - self._max_event_age:
            # Privacy retention also covers delayed broker delivery: do not
            # resurrect old traces after the database retention job removed them.
            self.queue.ack(delivery, outcome='expired')
            return True
        try:
            outcome = self._insert(delivery.record)
        except Exception as exc:
            permanent = isinstance(exc, InvalidTrace) or (isinstance(exc, psycopg2.Error)
                and (exc.pgcode or '')[:2] in {'22', '23'})
            self._count('database_errors' if isinstance(exc, psycopg2.Error) else 'consumer_errors')
            category = 'invalid_trace' if permanent else 'database_unavailable'
            delay = min(300, self._retry_base * 2 ** min(delivery.attempts - 1, 10))
            self.queue.retry(delivery, delay=delay, category=category, permanent=permanent)
            log.warning('trace_delivery_retry category=%s attempt=%d', category, delivery.attempts)
        else:
            self.queue.ack(delivery, outcome=outcome)
        return True

    def check_database(self) -> bool:
        try:
            pool = self._connection_pool()
            conn = pool.getconn()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute('SELECT 1')
                    return cur.fetchone()[0] == 1
            finally:
                pool.putconn(conn, close=bool(conn.closed))
        except psycopg2.Error:
            return False

    def _work(self):
        while not self._stop.is_set():
            try:
                if self.process_one():
                    continue
            except Exception as exc:
                self._count('consumer_errors')
                category = self._queue_error(exc) if isinstance(exc, sqlite3.Error) else type(exc).__name__
                log.warning('trace_consumer_failed category=%s', category)
            self._wake.wait(0.2)
            self._wake.clear()

    def close(self):
        """Bounded drain; unfinished events remain for another consumer."""
        self._closed = True
        deadline = time.monotonic() + self._shutdown_seconds
        # SQS already owns the durable backlog. Do not make network depth calls
        # or wait for that backlog to drain during local shutdown. A long poll
        # may outlive this join deadline; its daemon thread is not ACKed by
        # shutdown, so an interrupted delivery recovers after visibility expiry.
        if self._threads and not isinstance(self.queue, SQSQueue):
            self._wake.set()
            while time.monotonic() < deadline:
                state = self.stats()
                if not state.get('pending', 0) and not state.get('leased', 0):
                    break
                time.sleep(0.02)
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        # Do not close a connection underneath an outstanding bounded query.
        if self._pool is not None and not any(thread.is_alive() for thread in self._threads):
            self._pool.closeall()
