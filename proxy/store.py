"""Durable trace ingestion: a bounded local SQLite spool drained into Postgres.

submit() writes the trace to a SQLite file in WAL mode and returns. Once it returns True the
trace survives the proxy process being killed (it is in the WAL; an OS crash or power loss can
still lose the last moments because synchronous=NORMAL does not fsync every commit).

A single drainer thread moves spooled traces to Postgres in batches. Each trace carries a
proxy generated trace_id with a unique index, and inserts use ON CONFLICT DO NOTHING, so a
batch that is written to Postgres but not yet deleted from the spool (crash in between) is
delivered again without creating duplicates.

Failure handling:
* Postgres unreachable or restarting: the batch stays in the spool and the drainer retries
  with capped exponential backoff, indefinitely, because the data is already durable.
* One record Postgres rejects (a data error, not a connection error): the batch is retried
  row by row so the other rows land; the bad row is moved to a dead letter table after
  MAX_RECORD_ATTEMPTS and counted. Nothing is discarded silently.
* Spool full (max_rows): submit() refuses the trace, logs an error and counts it as dropped.
  The request itself is never failed because of tracing.
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, execute_batch

log = logging.getLogger("agenteval.proxy.store")

FIELDS = (
    "trace_id", "request_id", "model", "response_model", "messages", "response", "status_code", "streamed",
    "prompt_tokens", "completion_tokens", "total_tokens", "latency_ms", "ttft_ms", "tool_calls",
    "error_type", "error", "case_ref", "grounded_eval", "redaction_count", "created_at",
)
JSON_FIELDS = {"messages", "response", "tool_calls"}
COLUMNS = ", ".join(FIELDS)
VALUES = ", ".join(
    "to_timestamp(%(created_at)s)" if f == "created_at" else f"%({f})s" for f in FIELDS
)
INSERT_SQL = f"INSERT INTO traces ({COLUMNS}) VALUES ({VALUES}) ON CONFLICT (trace_id) DO NOTHING"

MAX_RECORD_ATTEMPTS = 3
TRANSIENT_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def strip_nul(value):
    """Postgres text and jsonb cannot hold U+0000, so it is replaced before storage."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    return value


class TraceStore:
    def __init__(self, dsn: str, spool_path: str, max_rows: int = 50_000, batch_size: int = 100,
                 retry_base: float = 0.5, retry_max: float = 30.0, idle_poll: float = 0.2):
        self._dsn = dsn
        self._max_rows = max_rows
        self._batch_size = batch_size
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._idle_poll = idle_poll
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pg = None

        Path(spool_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(spool_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS spool (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, enqueued_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, payload TEXT NOT NULL)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS dead_letter (
            seq INTEGER PRIMARY KEY, trace_id TEXT NOT NULL, enqueued_at REAL NOT NULL,
            failed_at REAL NOT NULL, error TEXT, payload TEXT NOT NULL)""")
        self._depth = self._db.execute("SELECT count(*) FROM spool").fetchone()[0]

        self.counters = {"spooled": 0, "dropped": 0, "written": 0, "dead_lettered": 0,
                         "drain_failures": 0}
        self.recovered_at_start = self._depth
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self.postgres_ok: bool | None = None
        if self._depth:
            log.info("spool holds %s traces from a previous run, they will be written first", self._depth)

    # ---- request side -------------------------------------------------------------

    def submit(self, record: dict) -> bool:
        """Persist one trace to the local spool. Blocking (a local SQLite write), so call it
        off the event loop. Returns False if the trace could not be kept."""
        payload = json.dumps(strip_nul(record), default=str)
        with self._lock:
            if self._depth >= self._max_rows:
                self.counters["dropped"] += 1
                log.error("trace dropped, spool full", extra={"trace_id": record.get("trace_id"),
                                                               "spool_depth": self._depth})
                return False
            try:
                self._db.execute("INSERT INTO spool (trace_id, enqueued_at, payload) VALUES (?, ?, ?)",
                                 (record["trace_id"], time.time(), payload))
            except sqlite3.Error as exc:
                self.counters["dropped"] += 1
                log.error("trace dropped, spool write failed: %s", exc, extra={"trace_id": record.get("trace_id")})
                return False
            self._depth += 1
            self.counters["spooled"] += 1
        self._wake.set()
        return True

    # ---- drainer --------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="trace-drainer", daemon=True)
        self._thread.start()

    def close(self, drain_timeout: float = 5.0) -> None:
        """Stop the drainer. Tries to empty the spool for up to drain_timeout seconds; anything
        left stays on disk and is written on the next start."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
        deadline = time.monotonic() + drain_timeout
        while self.depth and time.monotonic() < deadline:
            if self.drain_once() == 0 and not self.postgres_ok:
                break
        if self.depth:
            log.warning("shutting down with %s traces still spooled; they will be written on restart", self.depth)
        self._close_pg()
        with self._lock:
            self._db.close()

    def _run(self) -> None:
        delay = self._retry_base
        while not self._stop.is_set():
            try:
                moved = self.drain_once()
            except Exception:  # noqa: BLE001  the drainer must keep running
                log.exception("unexpected drainer error")
                moved = 0
                self.postgres_ok = False
            if self.postgres_ok is False:
                # New submissions must not shorten the backoff, so wait on the stop event.
                self._stop.wait(delay)
                delay = min(delay * 2, self._retry_max)
                continue
            delay = self._retry_base
            if moved == 0:
                self._wake.wait(self._idle_poll)
                self._wake.clear()

    def drain_once(self) -> int:
        """Write one batch to Postgres. Returns the number of spooled rows removed."""
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, trace_id, attempts, payload FROM spool ORDER BY seq LIMIT ?", (self._batch_size,)
            ).fetchall()
        if not rows:
            return 0
        try:
            conn = self._connection()
            with conn, conn.cursor() as cur:
                execute_batch(cur, INSERT_SQL, [self._params(r[3]) for r in rows], page_size=self._batch_size)
            self._delete([r[0] for r in rows])
            self._mark_ok(len(rows))
            return len(rows)
        except TRANSIENT_ERRORS as exc:
            self._mark_down(exc)
            return 0
        except psycopg2.Error as exc:
            log.warning("batch rejected by Postgres, retrying row by row: %s", exc)
            return self._drain_individually(rows)

    def _drain_individually(self, rows) -> int:
        removed = []
        for seq, trace_id, attempts, payload in rows:
            try:
                conn = self._connection()
                with conn, conn.cursor() as cur:
                    cur.execute(INSERT_SQL, self._params(payload))
                removed.append(seq)
            except TRANSIENT_ERRORS as exc:
                self._mark_down(exc)
                break
            except psycopg2.Error as exc:
                self._record_failure(seq, trace_id, attempts + 1, payload, f"{type(exc).__name__}: {exc}"[:500])
        self._delete(removed)
        if removed:
            self._mark_ok(len(removed))
        return len(removed)

    def _record_failure(self, seq, trace_id, attempts, payload, error) -> None:
        with self._lock:
            if attempts >= MAX_RECORD_ATTEMPTS:
                self._db.execute("BEGIN")
                self._db.execute(
                    "INSERT INTO dead_letter (seq, trace_id, enqueued_at, failed_at, error, payload) "
                    "SELECT seq, trace_id, enqueued_at, ?, ?, payload FROM spool WHERE seq = ?",
                    (time.time(), error, seq))
                self._db.execute("DELETE FROM spool WHERE seq = ?", (seq,))
                self._db.execute("COMMIT")
                self._depth -= 1
                self.counters["dead_lettered"] += 1
                log.error("trace moved to dead letter after %s attempts: %s", attempts, error,
                          extra={"trace_id": trace_id})
            else:
                self._db.execute("UPDATE spool SET attempts = ?, last_error = ? WHERE seq = ?", (attempts, error, seq))

    def _delete(self, seqs: list[int]) -> None:
        if not seqs:
            return
        with self._lock:
            self._db.executemany("DELETE FROM spool WHERE seq = ?", [(s,) for s in seqs])
            self._depth -= len(seqs)

    def _params(self, payload: str) -> dict:
        record = json.loads(payload)
        params = {}
        for name in FIELDS:
            value = record.get(name)
            params[name] = Json(value) if name in JSON_FIELDS and value is not None else value
        params["grounded_eval"] = bool(record.get("grounded_eval"))
        params["streamed"] = bool(record.get("streamed"))
        params["redaction_count"] = record.get("redaction_count") or 0
        return params

    def _connection(self):
        if self._pg is None or self._pg.closed:
            self._pg = psycopg2.connect(self._dsn, connect_timeout=5, keepalives=1, keepalives_idle=10,
                                        options="-c statement_timeout=15000")
        return self._pg

    def _close_pg(self) -> None:
        if self._pg is not None:
            try:
                self._pg.close()
            except psycopg2.Error:
                pass
            self._pg = None

    def _mark_ok(self, count: int) -> None:
        if self.postgres_ok is False:
            log.info("Postgres reachable again, draining spool", extra={"spool_depth": self.depth})
        self.postgres_ok = True
        self.last_success_at = time.time()
        self.counters["written"] += count

    def _mark_down(self, exc: Exception) -> None:
        if self.postgres_ok is not False:
            log.warning("Postgres unavailable, traces stay in the spool: %s", str(exc).strip()[:200],
                        extra={"spool_depth": self.depth})
        self.postgres_ok = False
        self.last_error = f"{type(exc).__name__}: {str(exc).strip()[:200]}"
        self.counters["drain_failures"] += 1
        self._close_pg()

    # ---- introspection -------------------------------------------------------------

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def full(self) -> bool:
        return self._depth >= self._max_rows

    def oldest_age_seconds(self) -> float:
        with self._lock:
            row = self._db.execute("SELECT min(enqueued_at) FROM spool").fetchone()
        return max(0.0, time.time() - row[0]) if row and row[0] else 0.0

    def dead_letter_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT count(*) FROM dead_letter").fetchone()[0]

    def stats(self) -> dict:
        return {
            "spool_depth": self.depth,
            "spool_capacity": self._max_rows,
            "oldest_spooled_age_seconds": round(self.oldest_age_seconds(), 3),
            "dead_letter_rows": self.dead_letter_count(),
            "postgres_ok": self.postgres_ok,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "recovered_at_start": self.recovered_at_start,
            **self.counters,
        }
