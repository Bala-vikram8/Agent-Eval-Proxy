"""Asynchronous candidate-quality monitoring with persistent deduplicated alerts.

Use a narrowly scoped monitor database role; the web dashboard remains read-only.
No model provider is called. A cohort label comes from the operator reference
catalog through eval_cohort, never directly from caller-supplied metadata.
"""
import logging
import os
import signal
import socket
import sys
import threading
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, RealDictCursor

try:
    from .monitoring import RULE_VERSION, alert_fingerprint, compare_quality, comparison_details
except ImportError:
    from monitoring import RULE_VERSION, alert_fingerprint, compare_quality, comparison_details

log = logging.getLogger("agenteval.monitor")
MAX_ROWS = 20000


def database_url() -> str:
    path = os.getenv("DATABASE_URL_FILE")
    return Path(path).read_text().strip() if path else os.environ["DATABASE_URL"]


def is_healthy(conn, *, worker_id: str, max_age_seconds: float = 180) -> bool:
    """A running PID is not evidence that monitoring reached the database."""
    with conn.cursor() as cur:
        cur.execute("""SELECT last_seen > now()-make_interval(secs=>%s)
            FROM worker_heartbeats WHERE worker_id=%s AND worker_type='monitoring'""",
                    (max_age_seconds, worker_id))
        row = cur.fetchone()
    return bool(row and row[0])


def run_once(conn, *, now: datetime | None = None, window_hours: int = 24,
             min_samples: int = 30, min_drop: float = 0.15, alpha: float = 0.01,
             worker_id: str = "quality-monitor") -> dict:
    if (type(window_hours) is not int or not 1 <= window_hours <= 720
            or type(min_samples) is not int or min_samples < 30
            or not 0 < min_drop <= 1 or not 0 < alpha < 1):
        raise ValueError("Invalid quality monitoring configuration")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Monitoring time must include a timezone")
    end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    midpoint, begin = end - timedelta(hours=window_hours), end - timedelta(hours=window_hours * 2)
    # DISTINCT ON prevents retry/history rows from counting as independent tasks.
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT DISTINCT ON (t.id,e.evaluator_name,e.evaluator_version,e.config_version)
            t.id,t.agent_id,t.model,t.eval_cohort,t.created_at,e.evaluator_name,e.evaluator_version,
            e.config_version,e.score FROM evaluation_runs e JOIN traces t ON t.id=e.trace_id
            WHERE t.created_at >= %s AND t.created_at < %s AND e.score IS NOT NULL
            ORDER BY t.id,e.evaluator_name,e.evaluator_version,e.config_version,e.created_at DESC,e.id DESC
            LIMIT %s""", (begin, end, MAX_ROWS + 1))
        records = cur.fetchall()
    outcomes = defaultdict(int)
    groups = defaultdict(lambda: [[], []])
    if len(records) > MAX_ROWS:
        outcomes["capacity_exceeded"] = 1
    elif not records:
        outcomes["no_scored_data"] = 1
    else:
        for row in records:
            if not row["eval_cohort"]:
                outcomes["unknown_request_mix_samples"] += 1
                continue
            cohort = (row["agent_id"], row["model"], row["eval_cohort"], row["evaluator_name"],
                      row["evaluator_version"], row["config_version"])
            groups[cohort][int(row["created_at"] >= midpoint)].append(float(row["score"]))
    inserted = 0
    for cohort, (old, new) in groups.items():
        result = compare_quality(old, new, baseline_cohort=cohort, current_cohort=cohort,
                                 min_samples=min_samples, min_drop=min_drop, alpha=alpha)
        outcomes[result.status] += 1
        if result.status != "quality_deterioration":
            continue
        details = comparison_details(result, cohort=cohort, window_end=end.isoformat(),
                                     alpha=alpha, min_drop=min_drop)
        details["min_samples"] = min_samples
        with conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO alerts (agent_id,metric,rule_version,severity,message,details,fingerprint)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (fingerprint) DO NOTHING""",
                (cohort[0], "candidate_quality_deterioration", RULE_VERSION, "high", result.message,
                 Json(details), alert_fingerprint(cohort, end.isoformat(), rule_config={
                     "window_hours": window_hours, "min_samples": min_samples,
                     "min_drop": min_drop, "alpha": alpha})))
            inserted += cur.rowcount
    details = {"window_end": end.isoformat(), "window_hours": window_hours,
               "scored_rows": len(records), "cohorts": len(groups), "outcomes": dict(outcomes),
               "alerts_inserted": inserted,
               "limitations": "Candidate score deterioration is not causal model drift; independent tasks are assumed."}
    with conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO worker_heartbeats(worker_id,worker_type,last_seen,details)
            VALUES (%s,'monitoring',now(),%s) ON CONFLICT(worker_id) DO UPDATE
            SET last_seen=excluded.last_seen,details=excluded.details""", (worker_id, Json(details)))
    return details


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    interval = max(10.0, float(os.getenv("MONITOR_INTERVAL_SECONDS", "60")))
    config = {"window_hours": int(os.getenv("MONITOR_WINDOW_HOURS", "24")),
              "min_samples": int(os.getenv("MONITOR_MIN_SAMPLES", "30")),
              "min_drop": float(os.getenv("MONITOR_MIN_DROP", "0.15")),
              "alpha": float(os.getenv("MONITOR_ALPHA", "0.01")),
              "worker_id": os.getenv("WORKER_ID", "monitor-" + socket.gethostname())}
    if (not 1 <= config["window_hours"] <= 720 or config["min_samples"] < 30
            or not 0 < config["min_drop"] <= 1 or not 0 < config["alpha"] < 1):
        raise ValueError("Invalid quality monitoring configuration")
    if sys.argv[1:] == ["--health"]:
        try:
            with closing(psycopg2.connect(database_url(), connect_timeout=5,
                                         options="-c statement_timeout=5000")) as conn:
                healthy = is_healthy(conn, worker_id=config["worker_id"],
                                     max_age_seconds=max(180, interval * 3))
        except (psycopg2.Error, OSError, ValueError, KeyError):
            healthy = False
        raise SystemExit(0 if healthy else 1)
    if sys.argv[1:]:
        raise SystemExit("Usage: python monitor.py [--health]")
    while not stopped.is_set():
        try:
            with closing(psycopg2.connect(database_url(), connect_timeout=5,
                                         options="-c statement_timeout=10000")) as conn:
                conn.autocommit = True
                summary = run_once(conn, **config)
                log.info("monitor_tick cohorts=%s alerts_inserted=%s", summary["cohorts"], summary["alerts_inserted"])
        except (psycopg2.Error, OSError, ValueError, KeyError) as exc:
            log.warning("monitor_tick_failed error_type=%s", type(exc).__name__)
        stopped.wait(interval)


if __name__ == "__main__":
    main()
