"""Bounded, operator-invoked trace deletion. Dry-run is the default.

No timer or import side effect deletes data. Schedule this as a restricted
maintenance task only after reviewing the retention policy and backups.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys

import psycopg2


def purge_batch(conn, cutoff: datetime, *, batch_size: int = 500,
                agent_id: str | None = None, execute: bool = False) -> int:
    """Delete one committed batch, including dependent historical evaluations."""
    if cutoff.tzinfo is None:
        raise ValueError("retention cutoff must include a timezone")
    if not 1 <= batch_size <= 10000:
        raise ValueError("retention batch must be between 1 and 10000")
    with conn, conn.cursor() as cur:
        # Look ahead within a finite bound, allowing a locked oldest row to be
        # skipped while keeping deletion itself limited to batch_size rows.
        candidate_limit = batch_size if not execute else min(20000, batch_size * 2)
        cur.execute("""SELECT id,event_id FROM traces WHERE created_at < %s
            AND (%s IS NULL OR agent_id=%s)
            ORDER BY created_at,id LIMIT %s""",
            (cutoff, agent_id, agent_id, candidate_limit))
        candidates = cur.fetchall()
        if not candidates or not execute:
            return len(candidates)
        # Match ingestion's lock order: event advisory lock BEFORE any trace row
        # lock. Waiting on an event while holding its trace row can deadlock with
        # a consumer's INSERT ON CONFLICT, so never reverse these steps.
        event_ids = sorted({event_id for _, event_id in candidates if event_id is not None})
        cur.execute("SET LOCAL lock_timeout='5s'")
        cur.execute("SET LOCAL statement_timeout='30s'")
        for event_id in event_ids:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        ("agenteval_trace:" + event_id,))
        cur.execute("""SELECT id,event_id FROM traces
            WHERE id=ANY(%s) AND created_at < %s AND (%s IS NULL OR agent_id=%s)
              AND (event_id IS NULL OR event_id=ANY(%s))
            ORDER BY created_at,id LIMIT %s FOR UPDATE SKIP LOCKED""",
            ([row[0] for row in candidates], cutoff, agent_id, agent_id, event_ids, batch_size))
        selected = cur.fetchall()
        ids = [row[0] for row in selected]
        if not ids:
            return 0
        for _, event_id in selected:
            if event_id is not None:
                cur.execute("INSERT INTO trace_tombstones(event_id) VALUES (%s) ON CONFLICT DO NOTHING", (event_id,))
        # Version-one databases retain the legacy FK table. Delete only child
        # rows for this batch; never drop or truncate a user's historical table.
        cur.execute("SELECT to_regclass('evaluations')")
        if cur.fetchone()[0] is not None:
            cur.execute("DELETE FROM evaluations WHERE trace_id=ANY(%s)", (ids,))
        cur.execute("DELETE FROM traces WHERE id=ANY(%s)", (ids,))
        return cur.rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=int(os.getenv("TRACE_RETENTION_DAYS", "30")))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--agent-id")
    parser.add_argument("--execute", action="store_true", help="actually delete; default only counts one batch")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 36500 or not 1 <= args.max_batches <= 1000:
        parser.error("days must be 1..36500 and max-batches must be 1..1000")
    dsn = os.getenv("RETENTION_DATABASE_URL")
    if not dsn:
        parser.error("set RETENTION_DATABASE_URL securely for the maintenance role")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    affected = 0
    try:
        with psycopg2.connect(dsn, connect_timeout=5, application_name="agenteval-retention") as conn:
            for _ in range(args.max_batches if args.execute else 1):
                count = purge_batch(conn, cutoff, batch_size=args.batch_size,
                                    agent_id=args.agent_id, execute=args.execute)
                affected += count
                if count < args.batch_size:
                    break
        print(json.dumps({"operation": "deleted" if args.execute else "dry_run_candidates",
                          "count": affected, "cutoff": cutoff.isoformat(),
                          "bounded": True}))
        return 0
    except (psycopg2.Error, ValueError) as exc:
        print(f"retention_failed category={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
