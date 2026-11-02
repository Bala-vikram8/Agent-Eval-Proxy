"""Retention is an explicit bounded maintenance action, never an import side effect."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sys
import time
import uuid

import psycopg2
import pytest

from conftest import ROOT, apply_schema, fetch

sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "proxy"))
from retention import purge_batch
from queueing import SQLiteQueue
from store import TraceStore


def seed(dsn):
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO traces(created_at,agent_id) VALUES
            (now()-interval '90 days','old'),(now()-interval '90 days','other'),(now(),'old')
            RETURNING id""")
        return [row[0] for row in cur.fetchall()]


def test_retention_dry_run_then_scoped_bounded_delete(db):
    old, other, recent = seed(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    with contextlib.closing(psycopg2.connect(db)) as conn:
        assert purge_batch(conn, cutoff, agent_id="old") == 1
        assert fetch(db, "SELECT count(*) AS n FROM traces")[0]["n"] == 3
        assert purge_batch(conn, cutoff, agent_id="old", execute=True) == 1
    remaining = {r["id"] for r in fetch(db, "SELECT id FROM traces")}
    assert remaining == {other, recent}


def test_retention_cascades_history_and_handles_legacy_foreign_key(make_database):
    dsn = make_database()
    apply_schema(dsn, ROOT / "tests/fixtures/schema_v1.sql")
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn, conn.cursor() as cur:
        cur.execute("INSERT INTO traces(created_at) VALUES (now()-interval '90 days') RETURNING id")
        trace_id = cur.fetchone()[0]
        cur.execute("INSERT INTO evaluations(trace_id,faithfulness_score,judge_reason) VALUES (%s,1,'synthetic')", (trace_id,))
    apply_schema(dsn)
    assert fetch(dsn, "SELECT count(*) AS n FROM evaluation_runs")[0]["n"] >= 1
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        assert purge_batch(conn, datetime.now(timezone.utc) - timedelta(days=30), execute=True) == 1
        assert purge_batch(conn, datetime.now(timezone.utc) - timedelta(days=30), execute=True) == 0
    assert fetch(dsn, "SELECT count(*) AS n FROM evaluation_runs")[0]["n"] == 0
    assert fetch(dsn, "SELECT count(*) AS n FROM evaluations")[0]["n"] == 0


def test_retention_respects_batch_bound_and_skips_locked_rows(db):
    old, other, _ = seed(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    with contextlib.closing(psycopg2.connect(db)) as locked, locked.cursor() as cur:
        cur.execute("SELECT id FROM traces WHERE id=%s FOR UPDATE", (old,))
        with contextlib.closing(psycopg2.connect(db)) as conn:
            assert purge_batch(conn, cutoff, batch_size=1, execute=True) == 1
        locked.rollback()
    remaining = {r["id"] for r in fetch(db, "SELECT id FROM traces")}
    assert old in remaining and other not in remaining


def test_retention_validates_bounds_before_deleting(db):
    with contextlib.closing(psycopg2.connect(db)) as conn:
        with pytest.raises(ValueError, match="timezone"):
            purge_batch(conn, datetime.now())
        with pytest.raises(ValueError, match="batch"):
            purge_batch(conn, datetime.now(timezone.utc), batch_size=0)


def test_deleted_event_cannot_be_recreated_by_queue_replay(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT_MODE", "redacted")
    queue = SQLiteQueue(str(tmp_path / "replay.sqlite3"))
    store = TraceStore(db, workers=1, queue=queue, embedded=False)
    event_id = str(uuid.uuid4())
    record = {"event_id": event_id, "model": "synthetic", "status_code": 200,
              "streamed": False, "captured_at": time.time() - 100,
              "messages": [{"role": "user", "content": "synthetic confidential test"}], "response": {}}
    try:
        assert store.submit(record) and store.process_one()
        with contextlib.closing(psycopg2.connect(db)) as conn:
            assert purge_batch(conn, datetime.now(timezone.utc) - timedelta(seconds=50), execute=False) == 1
            assert fetch(db, "SELECT event_id FROM trace_tombstones WHERE event_id=%s", (event_id,)) == []
            assert purge_batch(conn, datetime.now(timezone.utc) - timedelta(seconds=50), execute=True) == 1
        assert fetch(db, "SELECT event_id FROM trace_tombstones WHERE event_id=%s", (event_id,)) == [{"event_id": event_id}]
        assert store.submit(record) and store.process_one(), "delayed delivery is accepted then safely ACKed"
        assert store.stats()["erased"] == 1
        assert fetch(db, "SELECT id FROM traces WHERE event_id=%s", (event_id,)) == []
    finally:
        store.close()


def test_concurrent_replay_and_retention_share_event_lock_order(db, tmp_path):
    event_id = str(uuid.uuid4())
    store = TraceStore(db, workers=1, queue=SQLiteQueue(str(tmp_path / "concurrent.sqlite3")), embedded=False)
    record = {"event_id": event_id, "model": "synthetic", "status_code": 200,
              "streamed": False, "captured_at": time.time() - 100}
    assert store._insert(record) == "delivered"
    def erase():
        with contextlib.closing(psycopg2.connect(db)) as conn:
            return purge_batch(conn, datetime.now(timezone.utc) - timedelta(seconds=50), execute=True)
    try:
        # Hold the common event lock until both operations are waiting. Either
        # transaction may win after release; neither ordering may resurrect data.
        with contextlib.closing(psycopg2.connect(db)) as gate, gate.cursor() as cur, ThreadPoolExecutor(max_workers=2) as pool:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("agenteval_trace:" + event_id,))
            replay = pool.submit(store._insert, record)
            deletion = pool.submit(erase)
            time.sleep(.05)
            assert not replay.done() and not deletion.done()
            gate.commit()
            assert replay.result(timeout=10) in {"delivered", "erased"}
            assert deletion.result(timeout=10) == 1
        assert store._insert(record) == "erased"
        assert fetch(db, "SELECT id FROM traces WHERE event_id=%s", (event_id,)) == []
    finally:
        store.close()
