"""Writes traces to Postgres from a small thread pool.

Writes happen after the response has been sent and are never allowed to fail a request:
if Postgres is down the trace is dropped and a warning is logged.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool

log = logging.getLogger("agenteval.proxy")

INSERT_SQL = """
INSERT INTO traces (
    request_id, model, messages, response, status_code, streamed,
    prompt_tokens, completion_tokens, total_tokens, latency_ms, ttft_ms, tool_calls
) VALUES (
    %(request_id)s, %(model)s, %(messages)s, %(response)s, %(status_code)s, %(streamed)s,
    %(prompt_tokens)s, %(completion_tokens)s, %(total_tokens)s, %(latency_ms)s, %(ttft_ms)s, %(tool_calls)s
)
"""

FIELDS = (
    "request_id", "model", "messages", "response", "status_code", "streamed",
    "prompt_tokens", "completion_tokens", "total_tokens", "latency_ms", "ttft_ms", "tool_calls",
)
JSON_FIELDS = {"messages", "response", "tool_calls"}


class TraceStore:
    def __init__(self, dsn: str, workers: int = 4):
        self._dsn = dsn
        self._workers = workers
        # One connection per worker thread, so the pool can never be exhausted.
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="trace-writer")
        self._pool: ThreadedConnectionPool | None = None
        self._lock = threading.Lock()

    def submit(self, record: dict) -> None:
        """Queue a trace for writing. Returns immediately."""
        self._executor.submit(self._write, record)

    def close(self) -> None:
        self._executor.shutdown(wait=True)
        if self._pool is not None:
            self._pool.closeall()

    def _connection_pool(self) -> ThreadedConnectionPool:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadedConnectionPool(1, self._workers, self._dsn)
            return self._pool

    def _write(self, record: dict) -> None:
        params = {}
        for name in FIELDS:
            value = record.get(name)
            params[name] = Json(value) if name in JSON_FIELDS and value is not None else value
        try:
            pool = self._connection_pool()
            conn = pool.getconn()
        except psycopg2.Error as exc:
            log.warning("trace dropped, cannot connect to Postgres: %s", exc)
            return
        broken = False
        try:
            with conn, conn.cursor() as cur:
                cur.execute(INSERT_SQL, params)
        except Exception as exc:  # noqa: BLE001  logging must never take the proxy down
            broken = conn.closed != 0
            log.warning("trace dropped, insert failed: %s", exc)
        finally:
            pool.putconn(conn, close=broken)
