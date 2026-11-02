"""Durable at-least-once trace transports.

SQLite is for one persistent filesystem (local development or a single host), not
a distributed filesystem or a replacement for replicated SQS. SQS visibility and
redrive policy belong to infrastructure; successful receive is never an ACK.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Delivery:
    event_id: str
    record: dict
    token: str
    attempts: int


class SQLiteQueue:
    def __init__(self, path: str, *, capacity: int = 10000, max_bytes: int = 256 * 1024 * 1024,
                 max_message_bytes: int = 240 * 1024, lease_seconds: float = 30,
                 max_attempts: int = 8, clock=time.time):
        if min(capacity, max_bytes, max_message_bytes, lease_seconds, max_attempts) <= 0:
            raise ValueError("Queue bounds must be positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Queue path must not be a symlink")
        # Set restrictive mode before SQLite can create a WAL/SHM file. SQLite
        # derives sidecar permissions from the database file's existing mode.
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Queue path must be a regular file owned by the application")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.capacity, self.max_bytes = capacity, max_bytes
        self.max_message_bytes, self.lease_seconds = max_message_bytes, lease_seconds
        self.max_attempts, self.clock = max_attempts, clock
        # SQLite serializes writers across processes. A bounded FIFO gate also
        # prevents new requests in this process from overtaking older waiters.
        # Only short SQLite operations use it; provider/DB calls never do.
        self._operation_condition = threading.Condition()
        self._operation_waiters = deque()
        self._operation_busy = False
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, bytes INTEGER NOT NULL,
                    created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL,
                    token TEXT, lease_until REAL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS events_ready ON events(state, due, created);
                CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self):
        def busy_error():
            error = sqlite3.OperationalError('Local queue operation is busy')
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            error.sqlite_errorname = 'SQLITE_BUSY'
            return error
        token = object()
        deadline = time.monotonic() + 2
        with self._operation_condition:
            if len(self._operation_waiters) >= 128:
                raise busy_error()
            self._operation_waiters.append(token)
            try:
                while self._operation_busy or self._operation_waiters[0] is not token:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise busy_error()
                    self._operation_condition.wait(remaining)
            except BaseException:
                self._operation_waiters.remove(token)
                self._operation_condition.notify_all()
                raise
            self._operation_waiters.popleft()
            self._operation_busy = True
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=2, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA wal_autocheckpoint=100")
            yield conn
        finally:
            try:
                if conn is not None:
                    conn.close()
            finally:
                with self._operation_condition:
                    self._operation_busy = False
                    self._operation_condition.notify_all()

    @staticmethod
    def _increment(conn, name: str, value: int = 1):
        conn.execute("INSERT INTO counters VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                     (name, value))

    def enqueue(self, record: dict) -> bool:
        payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        size = len(payload.encode())
        event_id = record["event_id"]
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT state FROM events WHERE event_id=?", (event_id,)).fetchone()
            if existing:
                conn.commit()
                return existing["state"] != "dead"
            count, used = conn.execute("SELECT count(*),coalesce(sum(bytes),0) FROM events").fetchone()
            if size > self.max_message_bytes or count >= self.capacity or used + size > self.max_bytes:
                self._increment(conn, "rejected")
                conn.commit()
                return False
            conn.execute("INSERT INTO events(event_id,payload,bytes,created,due) VALUES (?,?,?,?,?)",
                         (event_id, payload, size, now, now))
            self._increment(conn, "accepted")
            conn.commit()
        return True

    def claim(self) -> Delivery | None:
        now, token = self.clock(), uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # A crashed worker already consumed an attempt; do not retry it forever.
            exhausted = conn.execute("""UPDATE events SET state='dead', token=NULL, lease_until=NULL,
                error='lease_attempts_exhausted' WHERE state='leased' AND lease_until<=? AND attempts>=?""",
                                     (now, self.max_attempts)).rowcount
            if exhausted:
                self._increment(conn, "dead_lettered", exhausted)
            row = conn.execute("""SELECT * FROM events WHERE
                (state='pending' AND due<=?) OR (state='leased' AND lease_until<=? AND attempts<?)
                ORDER BY created,event_id LIMIT 1""", (now, now, self.max_attempts)).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute("UPDATE events SET state='leased', token=?,lease_until=?,attempts=attempts+1 WHERE event_id=?",
                         (token, now + self.lease_seconds, row["event_id"]))
            conn.commit()
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError, RecursionError):
            payload = {"invalid_payload": True}
        return Delivery(row["event_id"], payload, token, row["attempts"] + 1)

    def ack(self, delivery: Delivery, *, outcome: str = "delivered") -> bool:
        if outcome not in {"delivered", "expired", "erased"}:
            raise ValueError("Invalid acknowledgement outcome")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            removed = conn.execute("DELETE FROM events WHERE event_id=? AND token=? AND state='leased'",
                                   (delivery.event_id, delivery.token)).rowcount
            if removed:
                self._increment(conn, outcome)
            conn.commit()
        return bool(removed)

    def retry(self, delivery: Delivery, *, delay: float, category: str, permanent: bool = False) -> bool:
        dead = permanent or delivery.attempts >= self.max_attempts
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("""UPDATE events SET state=?,due=?,token=NULL,lease_until=NULL,error=?
                WHERE event_id=? AND token=? AND state='leased'""",
                                   ("dead" if dead else "pending", self.clock() + max(delay, 0),
                                    category[:80], delivery.event_id, delivery.token)).rowcount
            if changed:
                self._increment(conn, "dead_lettered" if dead else "retried")
            conn.commit()
        return bool(changed)

    def stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute("SELECT state,count(*) AS n FROM events GROUP BY state").fetchall()
            oldest, size = conn.execute("SELECT min(created),coalesce(sum(bytes),0) FROM events").fetchone()
            counters = dict(conn.execute("SELECT name,value FROM counters"))
        counts = {"pending": 0, "leased": 0, "dead": 0, **{r["state"]: r["n"] for r in rows}}
        return {**counts, "expired": 0, "erased": 0, **counters, "backend": "sqlite", "bytes": size,
                "capacity": self.capacity, "max_bytes": self.max_bytes,
                "accepting": sum(counts.values()) < self.capacity and size < self.max_bytes,
                "oldest_age_seconds": max(0, self.clock() - oldest) if oldest is not None else 0}

    def purge_before(self, cutoff: float, *, execute: bool = True) -> int:
        """Administrative privacy operation; caller must authorize the cutoff.

        Drops queued payloads as well as dead letters. Deleted events cannot be
        delivered. Callers must report that intentional loss to the operator.
        """
        if not math.isfinite(cutoff):
            raise ValueError("Queue purge cutoff must be finite")
        with self._connect() as conn:
            if not execute:
                return conn.execute("SELECT count(*) FROM events WHERE created<?", (cutoff,)).fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            removed = conn.execute("DELETE FROM events WHERE created<?", (cutoff,)).rowcount
            if removed:
                self._increment(conn, "purged", removed)
            conn.commit()
            return removed

    def redrive_dead(self, event_id: str, *, execute: bool = False) -> int:
        """Operator replay after repair; never automatically reset attempt caps."""
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
            raise ValueError("Invalid redrive event identifier")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("SELECT 1 FROM events WHERE event_id=? AND state='dead'",
                                  (event_id,)).fetchone()
            if not exists or not execute:
                conn.commit()
                return int(bool(exists))
            conn.execute("""UPDATE events SET state='pending',attempts=0,due=?,
                token=NULL,lease_until=NULL,error=NULL WHERE event_id=? AND state='dead'""",
                         (self.clock(), event_id))
            self._increment(conn, "redriven")
            conn.commit()
            return 1


class SQSQueue:
    """SQS Standard transport; duplicates are handled by the Postgres event key.

    Queue encryption, retention, visibility, maxReceiveCount and DLQ are required
    deployment configuration. This adapter does not create or alter cloud queues.
    """
    def __init__(self, url: str, *, client=None, max_message_bytes: int = 240 * 1024,
                 lease_seconds: int = 30, capacity: int = 10000, wait_seconds: int = 20):
        if not url:
            raise ValueError("TRACE_SQS_QUEUE_URL is required")
        if min(max_message_bytes, lease_seconds, capacity) <= 0 or lease_seconds > 43200:
            raise ValueError("Invalid SQS limits")
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 20:
            raise ValueError("SQS long-poll wait must be an integer from zero to 20 seconds")
        poll_client = client
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("sqs", config=Config(connect_timeout=3, read_timeout=5,
                                                      retries={"max_attempts": 2, "mode": "standard"}))
            # A receive may wait for the full long-poll interval. Keep health,
            # publish and acknowledgement calls on a separately bounded client.
            poll_client = boto3.client("sqs", config=Config(connect_timeout=3,
                read_timeout=max(5, wait_seconds + 5),
                retries={"total_max_attempts": 1, "mode": "standard"}))
        self.url, self.client = url, client
        self.poll_client, self.wait_seconds = poll_client, wait_seconds
        self.max_message_bytes, self.lease_seconds = max_message_bytes, lease_seconds
        self.capacity = capacity
        self.accepted = self.rejected = self.delivered = self.retried = self.expired = self.erased = 0
        self._validate_configuration()

    def _validate_configuration(self):
        attrs = self.client.get_queue_attributes(QueueUrl=self.url,
            AttributeNames=["RedrivePolicy", "SqsManagedSseEnabled", "KmsMasterKeyId", "FifoQueue"])["Attributes"]
        try:
            redrive = json.loads(attrs.get("RedrivePolicy", "{}"))
            maximum = int(redrive.get("maxReceiveCount", 0))
            destination = redrive.get("deadLetterTargetArn", "")
        except (ValueError, TypeError, AttributeError):
            raise ValueError("SQS requires a valid managed dead-letter redrive policy") from None
        if not 1 <= maximum <= 1000 or not isinstance(destination, str) or not destination.startswith("arn:"):
            raise ValueError("SQS requires a managed dead-letter redrive policy")
        if attrs.get("SqsManagedSseEnabled") != "true" and not attrs.get("KmsMasterKeyId"):
            raise ValueError("SQS requires encryption at rest")
        if attrs.get("FifoQueue") == "true":
            raise ValueError("The ingestion adapter requires an SQS Standard queue")

    def enqueue(self, record: dict) -> bool:
        payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > self.max_message_bytes:
            self.rejected += 1
            return False
        # SQS gauges are eventually consistent: this is an admission guard,
        # not an exact distributed queue bound. The local spool is exact.
        if not self.stats()["accepting"]:
            self.rejected += 1
            return False
        self.client.send_message(QueueUrl=self.url, MessageBody=payload)
        self.accepted += 1
        return True

    def claim(self) -> Delivery | None:
        response = self.poll_client.receive_message(QueueUrl=self.url, MaxNumberOfMessages=1,
            VisibilityTimeout=self.lease_seconds, WaitTimeSeconds=self.wait_seconds,
            MessageSystemAttributeNames=["ApproximateReceiveCount"])
        rows = response.get("Messages", [])
        if not rows:
            return None
        row = rows[0]
        try:
            payload = json.loads(row["Body"])
        except (ValueError, TypeError, RecursionError):
            payload = {"invalid_payload": True}
        event_id = payload.get("event_id", "invalid") if isinstance(payload, dict) else "invalid"
        return Delivery(event_id, payload, row["ReceiptHandle"],
                        int(row.get("Attributes", {}).get("ApproximateReceiveCount", 1)))

    def ack(self, delivery: Delivery, *, outcome: str = "delivered") -> bool:
        if outcome not in {"delivered", "expired", "erased"}:
            raise ValueError("Invalid acknowledgement outcome")
        self.client.delete_message(QueueUrl=self.url, ReceiptHandle=delivery.token)
        if outcome == "erased":
            self.erased += 1
        elif outcome == "expired":
            self.expired += 1
        else:
            self.delivered += 1
        return True

    def retry(self, delivery: Delivery, *, delay: float, category: str, permanent: bool = False) -> bool:
        # Do not delete poison data: allow the queue's managed redrive policy to
        # move it to its encrypted DLQ after the configured receive count.
        self.client.change_message_visibility(QueueUrl=self.url, ReceiptHandle=delivery.token,
                                              VisibilityTimeout=min(43200, max(1, int(delay))))
        self.retried += 1
        return True

    def stats(self) -> dict:
        result = self.client.get_queue_attributes(QueueUrl=self.url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                            "ApproximateNumberOfMessagesDelayed"])
        attrs = result.get("Attributes", {})
        pending = int(attrs.get("ApproximateNumberOfMessages", 0))
        leased = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
        delayed = int(attrs.get("ApproximateNumberOfMessagesDelayed", 0))
        return {"backend": "sqs", "pending": pending, "leased": leased, "delayed": delayed,
                "accepted": self.accepted, "rejected": self.rejected,
                "delivered": self.delivered, "retried": self.retried, "expired": self.expired, "erased": self.erased,
                "accepting": pending + leased + delayed < self.capacity,
                "capacity": self.capacity, "counts_approximate": True,
                "admission_scope": "eventually_consistent_backlog_estimate"}
