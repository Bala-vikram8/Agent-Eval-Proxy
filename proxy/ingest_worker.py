"""Separate ingestion service for a retained spool or existing SQS queue."""
import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path

from store import TraceStore
from queueing import SQLiteQueue


def health_path() -> Path:
    return Path(os.getenv('TRACE_INGEST_HEALTH_PATH', '/tmp/agenteval-ingest-health.json'))


def healthy() -> bool:
    try:
        data = json.loads(health_path().read_text())
        return 0 <= time.time() - data['at'] < 30 and data['database_ok'] and not data['queue_unavailable']
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--health', action='store_true')
    action.add_argument('--purge-older-than-days', type=float,
                        help='review an explicit local spool/DLQ retention cutoff; default is dry-run')
    action.add_argument('--redrive-event', help='review replay of one dead local event after fixing the failure')
    parser.add_argument('--execute', action='store_true', help='actually delete the reviewed local spool batch')
    args = parser.parse_args()
    if args.execute and args.purge_older_than_days is None and args.redrive_event is None:
        parser.error('--execute requires an explicit purge or redrive action')
    if args.health:
        return 0 if healthy() else 1
    if args.purge_older_than_days is not None or args.redrive_event is not None:
        if args.purge_older_than_days is not None and not 1 <= args.purge_older_than_days <= 36500:
            parser.error('purge days must be between 1 and 36500')
        if os.getenv('TRACE_QUEUE_BACKEND', 'sqlite') != 'sqlite' or not os.getenv('TRACE_SPOOL_PATH'):
            parser.error('local purge requires TRACE_QUEUE_BACKEND=sqlite and an explicit TRACE_SPOOL_PATH')
        queue = SQLiteQueue(os.environ['TRACE_SPOOL_PATH'])
        if args.redrive_event is not None:
            count = queue.redrive_dead(args.redrive_event, execute=args.execute)
            print(json.dumps({'operation': 'redriven' if args.execute else 'dry_run_candidates',
                              'count': count, 'scope': 'one_local_dead_letter',
                              'warning': 'Replay can deliver an event previously deleted from the database; review retention first.'}))
            return 0
        count = queue.purge_before(time.time() - args.purge_older_than_days * 86400, execute=args.execute)
        print(json.dumps({'operation': 'purged' if args.execute else 'dry_run_candidates',
                          'count': count, 'scope': 'local_spool_including_dead_letters',
                          'warning': 'Coordinate active workers and database retention; queue deletion alone is not end-to-end erasure.'}))
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    store = TraceStore(os.environ['DATABASE_URL'], workers=4, embedded=True)
    path = health_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        while not stop.is_set():
            stats = store.stats()
            data = {'at': time.time(), 'database_ok': store.check_database(),
                    'queue_unavailable': bool(stats.get('queue_unavailable')), 'queue': stats}
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data))
            os.chmod(temporary, 0o600)
            temporary.replace(path)
            stop.wait(5)
    finally:
        store.close()
        path.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
