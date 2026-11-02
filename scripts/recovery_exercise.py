"""Local synthetic ingestion outage/crash/replay exercise; no cloud resources.

Creates one temporary database and private spool, kills only the worker it
started, and removes only that database/spool. Requires a loopback test DSN.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import psycopg2
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))
from store import TraceStore  # noqa: E402


def row_count(dsn):
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM traces")
        return cur.fetchone()[0]


def wait_for(check, *, child, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Synthetic ingestion worker exited; inspect its log")
        if check():
            return
        time.sleep(.05)
    raise TimeoutError("Synthetic recovery condition was not satisfied")


def run(samples, output):
    admin = os.environ.get("TEST_DATABASE_URL", "")
    if not admin or psycopg2.extensions.parse_dsn(admin).get("host") not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Set TEST_DATABASE_URL to a loopback development database administrator.")
    output.mkdir(parents=True, exist_ok=True)
    database = "agenteval_recovery_" + uuid.uuid4().hex[:12]
    dsn = psycopg2.extensions.make_dsn(admin, dbname=database)
    with contextlib.closing(psycopg2.connect(admin)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    child, publisher = None, None
    try:
        with contextlib.closing(psycopg2.connect(dsn)) as conn, conn, conn.cursor() as cur:
            cur.execute((ROOT / "sql/init.sql").read_text())
        with tempfile.TemporaryDirectory(prefix="agenteval-recovery-") as work:
            settings = {"TRACE_QUEUE_BACKEND": "sqlite", "TRACE_SPOOL_PATH": str(Path(work) / "spool.sqlite3"),
                "TRACE_CONTENT_MODE": "metadata", "TRACE_INGEST_EMBEDDED": "false",
                "TRACE_INGEST_HEALTH_PATH": str(Path(work) / "health.json"),
                "TRACE_INGEST_LEASE_SECONDS": "15", "TRACE_RETRY_BASE_SECONDS": "0.2",
                "TRACE_SHUTDOWN_SECONDS": "1"}
            os.environ.update(settings)
            publisher = TraceStore(dsn, embedded=False)
            events = [{"event_id": str(uuid.uuid4()), "agent_id": "synthetic-recovery",
                       "model": "synthetic-only", "status_code": 200, "streamed": False}
                      for _ in range(samples)]
            assert all(publisher.submit(event) for event in events)
            with contextlib.closing(socket.socket()) as unused:
                unused.bind(("127.0.0.1", 0))
                port = unused.getsockname()[1]
            refused_dsn = f"postgresql://synthetic@127.0.0.1:{port}/unavailable"
            with (output / "unavailable-worker.log").open("w") as log:
                child = subprocess.Popen([sys.executable, "ingest_worker.py"], cwd=ROOT / "proxy",
                    env={**os.environ, "DATABASE_URL": refused_dsn}, stdout=log, stderr=subprocess.STDOUT)
                wait_for(lambda: publisher.stats().get("retried", 0) > 0, child=child, seconds=15)
                before = publisher.stats()
                assert row_count(dsn) == 0
                child.kill()
                child.wait(timeout=5)
                killed_exit = child.returncode
                child = None
            began = time.monotonic()
            with (output / "restarted-worker.log").open("w") as log:
                child = subprocess.Popen([sys.executable, "ingest_worker.py"], cwd=ROOT / "proxy",
                    env={**os.environ, "DATABASE_URL": dsn}, stdout=log, stderr=subprocess.STDOUT)
                def recovered():
                    state = publisher.stats()
                    return row_count(dsn) == samples and state.get("pending", 0) + state.get("leased", 0) == 0
                wait_for(recovered, child=child, seconds=45)
                recovered_seconds = time.monotonic() - began
                # Replay the same stable identities after successful ACK. This
                # must not create a second trace, even after a real process kill.
                assert all(publisher.submit(event) for event in events)
                wait_for(lambda: publisher.stats().get("delivered", 0) >= samples * 2,
                         child=child, seconds=20)
                final = publisher.stats()
                assert row_count(dsn) == samples and final.get("dead", 0) == 0
                child.terminate()
                child.wait(timeout=10)
                child = None
            report = {"simulated": True, "backend": "sqlite", "events": samples,
                "database_unavailable_retries_observed": before.get("retried", 0),
                "worker_killed_exit_code": killed_exit, "recovery_seconds": recovered_seconds,
                "configured_visibility_lease_seconds": 15, "unique_database_rows_after_duplicate_replay": samples,
                "final_queue": final, "provider_spend_usd": 0,
                "limitations": "Local retained filesystem and real worker process restart. Not an AWS/SQS or database-backup restore test."}
            (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
            publisher.close()
            publisher = None
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=5)
        if publisher is not None:
            publisher.close()
        with contextlib.closing(psycopg2.connect(admin)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("artifacts/recovery"))
    args = parser.parse_args()
    if not 1 <= args.samples <= 500:
        parser.error("samples must be between 1 and 500")
    run(args.samples, args.output)
