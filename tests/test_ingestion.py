"""Durable transport, failure recovery and idempotent ingestion evidence.

All payloads and credentials are synthetic. SQS tests use a local fake API and
do not establish real AWS authorization, replication or redrive behavior.
"""
import contextlib
import json
import sqlite3
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest

from conftest import ROOT, apply_schema, fetch, free_port

sys.path.insert(0, str(ROOT / "proxy"))
from queueing import SQLiteQueue, SQSQueue
from store import TraceStore
import ingest_worker


def event(identifier="event-one", **overrides):
    return {"event_id": identifier, "request_id": "upstream-id", "agent_id": "synthetic-service",
            "model": "gpt-4o-mini", "status_code": 200, "streamed": False,
            "captured_at": time.time(),
            "messages": [{"role": "user", "content": "Synthetic repair workflow"}],
            "response": {"choices": [{"message": {"content": "Inspect the synthetic checklist."}}]},
            **overrides}


def queue(tmp_path, **kwargs):
    return SQLiteQueue(str(tmp_path / "queue.sqlite3"), **kwargs)


def test_acceptance_survives_reopening_and_bounds_capacity(tmp_path):
    q = queue(tmp_path, capacity=1)
    assert q.enqueue(event())
    assert not q.enqueue(event("too-many"))
    reopened = queue(tmp_path, capacity=1)
    delivery = reopened.claim()
    assert delivery.record["event_id"] == "event-one"
    assert reopened.ack(delivery)
    assert reopened.enqueue(event("after-drain"))
    assert reopened.stats()["delivered"] == 1
    assert reopened.stats()["rejected"] == 1


def test_payload_byte_limits_are_enforced(tmp_path):
    q = queue(tmp_path, max_message_bytes=500, max_bytes=500)
    assert not q.enqueue(event(messages=[{"content": "x" * 1000}]))
    assert q.enqueue(event())
    assert not q.enqueue(event("two"))


def test_spool_and_wal_are_private_before_any_payload_is_written(tmp_path):
    q = queue(tmp_path)
    # Keep a reader open so the WAL persists while the second connection writes.
    with sqlite3.connect(q.path) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM events").fetchall()
        assert q.enqueue(event())
        for path in tmp_path.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    link = tmp_path / "link.sqlite3"
    link.symlink_to(q.path)
    with pytest.raises(ValueError, match="symlink"):
        SQLiteQueue(str(link))


def test_dead_letters_consume_capacity_and_admin_purge_is_reported(tmp_path):
    now = [100.0]
    q = queue(tmp_path, capacity=1, clock=lambda: now[0])
    q.enqueue(event())
    q.retry(q.claim(), delay=0, category="invalid_trace", permanent=True)
    assert not q.enqueue(event("no-room"))
    assert q.purge_before(101, execute=False) == 1
    assert q.stats()["dead"] == 1
    assert q.purge_before(101) == 1
    assert q.stats()["purged"] == 1
    assert queue(tmp_path).stats()["purged"] == 1
    assert q.enqueue(event("after-reviewed-purge"))


def test_dead_letter_replay_requires_operator_execution_and_preserves_cap(tmp_path):
    q = queue(tmp_path, max_attempts=1)
    q.enqueue(event())
    q.retry(q.claim(), delay=0, category="database_unavailable")
    assert q.stats()["dead"] == 1
    assert q.redrive_dead("event-one") == 1  # default dry run
    assert q.claim() is None
    assert q.redrive_dead("event-one", execute=True) == 1
    delivery = q.claim()
    assert delivery.attempts == 1
    q.retry(delivery, delay=0, category="database_unavailable")
    assert q.stats()["dead"] == 1
    assert q.stats()["redriven"] == 1
    assert q.stats()["dead_lettered"] == 2
    assert q.redrive_dead("not-found", execute=True) == 0


def test_expired_lease_is_recovered_and_stale_ack_is_fenced(tmp_path):
    now = [100.0]
    q = queue(tmp_path, lease_seconds=5, max_attempts=2, clock=lambda: now[0])
    q.enqueue(event())
    original = q.claim()
    assert q.claim() is None
    now[0] += 6
    recovered = q.claim()
    assert recovered.attempts == 2 and recovered.token != original.token
    assert not q.ack(original)
    assert not q.retry(original, delay=0, category="stale")
    assert q.ack(recovered)
    assert q.stats()["delivered"] == 1


def test_crash_attempt_cap_and_retry_exhaustion_reach_dead_letter(tmp_path):
    now = [100.0]
    q = queue(tmp_path, lease_seconds=5, max_attempts=2, clock=lambda: now[0])
    q.enqueue(event())
    q.claim()  # crash before committing downstream
    now[0] += 6
    q.claim()  # another crash
    now[0] += 6
    assert q.claim() is None
    assert q.stats()["dead"] == 1
    assert q.stats()["dead_lettered"] == 1
    # A second event fails explicitly, with a delayed retry.
    q.enqueue(event("second"))
    first = q.claim()
    q.retry(first, delay=3, category="database_unavailable")
    assert q.claim() is None
    now[0] += 4
    second = q.claim()
    q.retry(second, delay=3, category="database_unavailable")
    assert q.stats()["dead"] == 2


def test_separate_consumers_do_not_share_claims(tmp_path):
    q = queue(tmp_path)
    for i in range(20):
        q.enqueue(event(str(i)))
    def receive(_):
        return queue(tmp_path).claim()
    with ThreadPoolExecutor(max_workers=8) as pool:
        deliveries = list(pool.map(receive, range(20)))
    assert len({d.event_id for d in deliveries}) == 20
    assert all(d.attempts == 1 for d in deliveries)


def test_fifty_publishers_and_independent_consumer_keep_durable_acceptances(tmp_path):
    publisher = queue(tmp_path, capacity=1500)
    consumer = queue(tmp_path, capacity=1500)
    finished = threading.Event()
    consumed, errors = [], []
    def consume():
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                delivery = consumer.claim()
                if delivery is not None:
                    consumed.append(delivery.event_id)
                    assert consumer.ack(delivery)
                elif finished.is_set():
                    return
                else:
                    time.sleep(.002)
            errors.append('consumer deadline')
        except Exception as exc:
            errors.append(type(exc).__name__)
    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    try:
        with ThreadPoolExecutor(max_workers=50) as pool:
            accepted = list(pool.map(lambda i: publisher.enqueue(event(str(i))), range(1000)))
    finally:
        finished.set()
        worker.join(30)
    assert not worker.is_alive() and not errors, errors
    assert all(accepted)
    assert len(consumed) == len(set(consumed)) == 1000
    metrics = publisher.stats()
    assert metrics['accepted'] == metrics['delivered'] == 1000
    assert metrics['pending'] == metrics['leased'] == metrics['dead'] == 0


def test_privacy_applies_before_spool_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT_MODE", "redacted")
    q = queue(tmp_path)
    store = TraceStore("unused", queue=q, embedded=False)
    assert store.submit(event(messages=[{"content": "Email person@example.test; password=synthetic-password"}],
                              evaluation_context={"confidential": "synthetic-business-secret"}))
    record = q.claim().record
    serialized = json.dumps(record)
    assert "person@example.test" not in serialized
    assert "synthetic-password" not in serialized
    assert "synthetic-business-secret" not in serialized
    assert "REDACTED" in serialized
    store.close()
    for path in tmp_path.iterdir():
        assert b"person@example.test" not in path.read_bytes()


def test_default_metadata_minimizes_disk_payload(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACE_CONTENT_MODE", raising=False)
    q = queue(tmp_path)
    store = TraceStore("unused", queue=q, embedded=False)
    assert store.submit(event(messages=[{"content": "arbitrary secret business prose"}]))
    record = q.claim().record
    assert record["messages"] is None and record["response"] is None
    assert record["content_mode"] == "metadata"
    store.close()


def test_queue_unavailable_is_rejected_and_visible(tmp_path):
    q = queue(tmp_path)
    store = TraceStore("unused", queue=q, embedded=False)
    def unavailable(_):
        raise OSError("synthetic credential must not escape")
    q.enqueue = unavailable
    assert not store.submit(event())
    assert store.stats()["publish_errors"] == 1
    store.close()
    assert not store.submit(event("closed"))


def test_sqlite_busy_and_locked_are_visible_without_raw_exception_text(tmp_path, caplog):
    q = queue(tmp_path)
    original_stats = q.stats
    store = TraceStore('unused', queue=q, embedded=False)
    busy = sqlite3.OperationalError('synthetic-secret SQL and filesystem details')
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT
    locked = sqlite3.OperationalError('another synthetic-secret must remain private')
    locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
    def failed_enqueue(_):
        raise busy
    def failed_stats():
        raise locked
    q.enqueue = failed_enqueue
    assert store.submit(event()) is False
    q.stats = failed_stats
    metrics = store.stats()
    assert metrics['accepting'] is False and metrics['queue_unavailable'] is True
    assert metrics['queue_busy_errors'] == 1 and metrics['queue_locked_errors'] == 1
    assert metrics['queue_error_category'] == 'sqlite_locked'
    assert metrics['queue_errors'] == 2 and metrics['publish_errors'] == 1
    assert 'synthetic-secret' not in caplog.text
    assert 'sqlite_busy' in caplog.text
    q.stats = original_stats
    recovered = store.stats()
    assert recovered['accepting'] is True
    assert recovered.get('queue_unavailable', False) is False
    assert recovered['queue_errors'] == 2
    store.close()


@pytest.mark.parametrize("name,value", [
    ("TRACE_SHUTDOWN_SECONDS", "inf"), ("TRACE_SHUTDOWN_SECONDS", "31"),
    ("TRACE_RETRY_BASE_SECONDS", "nan"), ("TRACE_RETENTION_DAYS", "nan"),
    ("TRACE_EVENT_MAX_AGE_SECONDS", "inf"),
])
def test_invalid_lifecycle_limits_fail_before_workers_start(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        TraceStore("unused", queue=queue(tmp_path), embedded=False)


def test_local_spool_retention_cli_defaults_to_review(tmp_path, monkeypatch, capsys):
    q = queue(tmp_path, clock=lambda: time.time() - 2 * 86400)
    q.enqueue(event())
    monkeypatch.setenv("TRACE_QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("TRACE_SPOOL_PATH", str(q.path))
    monkeypatch.setattr(sys, "argv", ["ingest_worker", "--purge-older-than-days", "1"])
    assert ingest_worker.main() == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "dry_run_candidates"
    assert q.stats()["pending"] == 1
    monkeypatch.setattr(sys, "argv", ["ingest_worker", "--purge-older-than-days", "1", "--execute"])
    assert ingest_worker.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["operation"] == "purged" and report["count"] == 1
    assert q.stats()["pending"] == 0


def test_local_dead_letter_redrive_cli_requires_execution(tmp_path, monkeypatch, capsys):
    q = queue(tmp_path)
    q.enqueue(event())
    q.retry(q.claim(), delay=0, category="invalid_trace", permanent=True)
    monkeypatch.setenv("TRACE_QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("TRACE_SPOOL_PATH", str(q.path))
    monkeypatch.setattr(sys, "argv", ["ingest_worker", "--redrive-event", "event-one"])
    assert ingest_worker.main() == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "dry_run_candidates"
    assert q.stats()["dead"] == 1
    monkeypatch.setattr(sys, "argv", ["ingest_worker", "--redrive-event", "event-one", "--execute"])
    assert ingest_worker.main() == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "redriven"
    assert q.stats()["pending"] == 1


@pytest.mark.parametrize("age,db_ok,expected", [(1, True, True), (31, True, False), (-60, True, False), (1, False, False)])
def test_ingest_health_requires_recent_database_success(tmp_path, monkeypatch, age, db_ok, expected):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"at": time.time() - age, "database_ok": db_ok, "queue_unavailable": False}))
    monkeypatch.setenv("TRACE_INGEST_HEALTH_PATH", str(path))
    assert bool(ingest_worker.healthy()) is expected


@pytest.fixture
def ingest_db(make_database):
    dsn = make_database()
    apply_schema(dsn)
    return dsn


def test_real_database_outage_retains_delivery_then_recovers(tmp_path, ingest_db, monkeypatch):
    monkeypatch.setenv("TRACE_RETRY_BASE_SECONDS", "0")
    q = queue(tmp_path)
    unavailable = TraceStore(f"postgresql://nobody@127.0.0.1:{free_port()}/none", queue=q, embedded=False)
    assert unavailable.submit(event())
    assert unavailable.process_one()
    assert q.stats()["pending"] == 1
    unavailable.close()
    recovered = TraceStore(ingest_db, queue=queue(tmp_path), embedded=False)
    assert recovered.process_one()
    assert recovered.stats()["pending"] == 0
    assert fetch(ingest_db, "SELECT count(*) AS n FROM traces")[0]["n"] == 1
    recovered.close()


@pytest.mark.parametrize("producer_mode,consumer_mode", [("metadata", "redacted"), ("redacted", "metadata")])
def test_redelivery_can_only_strengthen_metadata_minimization(tmp_path, ingest_db, monkeypatch,
                                                           producer_mode, consumer_mode):
    q = queue(tmp_path)
    monkeypatch.setenv("TRACE_CONTENT_MODE", producer_mode)
    publisher = TraceStore(ingest_db, queue=q, embedded=False)
    assert publisher.submit(event())
    publisher.close()
    monkeypatch.setenv("TRACE_CONTENT_MODE", consumer_mode)
    consumer = TraceStore(ingest_db, queue=queue(tmp_path), embedded=False)
    assert consumer.process_one()
    [row] = fetch(ingest_db, "SELECT content_mode,messages,response,evaluation_context FROM traces")
    assert row["content_mode"] == "metadata"
    assert row["messages"] is None and row["response"] is None and row["evaluation_context"] is None
    consumer.close()


def test_commit_before_ack_crash_redelivers_without_duplicate_trace(tmp_path, ingest_db):
    now = [time.time()]
    q = queue(tmp_path, lease_seconds=5, clock=lambda: now[0])
    store = TraceStore(ingest_db, queue=q, embedded=False)
    assert store.submit(event())
    claimed = q.claim()
    store._insert(claimed.record)  # crash after commit, before transport ACK
    now[0] += 6
    assert store.process_one()
    assert fetch(ingest_db, "SELECT count(*) AS n FROM traces")[0]["n"] == 1
    assert q.stats()["delivered"] == 1
    store.close()


def test_poison_event_dead_letters_and_later_event_is_processed(tmp_path, ingest_db):
    q = queue(tmp_path)
    q.enqueue(event("invalid", status_code="not-an-integer"))
    q.enqueue(event("valid"))
    store = TraceStore(ingest_db, queue=q, embedded=False)
    assert store.process_one()
    assert store.process_one()
    assert q.stats()["dead"] == 1
    assert fetch(ingest_db, "SELECT event_id FROM traces") == [{"event_id": "valid"}]
    store.close()


def test_close_drains_successful_work_and_event_time_is_preserved(tmp_path, ingest_db, monkeypatch):
    monkeypatch.setenv("TRACE_SHUTDOWN_SECONDS", "3")
    store = TraceStore(ingest_db, queue=queue(tmp_path), workers=1, embedded=True)
    earlier = time.time() - 100
    assert store.submit(event(captured_at=earlier))
    store.close()
    [row] = fetch(ingest_db, "SELECT extract(epoch from created_at) AS at FROM traces")
    assert abs(float(row["at"]) - earlier) < 0.01


def test_expired_queue_payload_cannot_resurrect_retained_trace(tmp_path, ingest_db, monkeypatch):
    monkeypatch.setenv("TRACE_RETENTION_DAYS", "1")
    q = queue(tmp_path)
    store = TraceStore(ingest_db, queue=q, embedded=False)
    assert store.submit(event(captured_at=time.time() - 86401))
    assert store.process_one()
    assert store.stats()["expired"] == 1
    assert store.stats().get("delivered", 0) == 0
    assert fetch(ingest_db, "SELECT count(*) AS n FROM traces")[0]["n"] == 0
    store.close()
    assert queue(tmp_path).stats()["expired"] == 1


@pytest.mark.parametrize("captured", [None, True, "yesterday", -1, float("inf"), 10**1000])
def test_invalid_broker_capture_time_is_dead_lettered(tmp_path, ingest_db, captured):
    q = queue(tmp_path)
    record = event(captured_at=captured)
    if isinstance(captured, float):
        # Supported publishers reject non-finite JSON; emulate corrupted broker
        # data directly to exercise the consumer's permanent-error path.
        assert q.enqueue(event())
        with sqlite3.connect(q.path) as conn:
            conn.execute("UPDATE events SET payload=?", (json.dumps(record),))
    else:
        assert q.enqueue(record)
    store = TraceStore(ingest_db, queue=q, embedded=False)
    assert store.process_one()
    assert q.stats()["dead"] == 1
    assert fetch(ingest_db, "SELECT count(*) AS n FROM traces")[0]["n"] == 0
    store.close()


class FakeSQS:
    def __init__(self):
        self.calls = []
        self.body = None
    def send_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        self.body = kwargs["MessageBody"]
        return {"MessageId": "synthetic"}
    def receive_message(self, **kwargs):
        self.calls.append(("receive", kwargs))
        return {"Messages": [{"Body": self.body, "ReceiptHandle": "receipt",
                               "Attributes": {"ApproximateReceiveCount": "2"}}]} if self.body else {}
    def delete_message(self, **kwargs):
        self.calls.append(("delete", kwargs))
        self.body = None
    def change_message_visibility(self, **kwargs):
        self.calls.append(("visibility", kwargs))
    def get_queue_attributes(self, **kwargs):
        self.calls.append(("attributes", kwargs))
        return {"Attributes": {"ApproximateNumberOfMessages": "1" if self.body else "0",
            "SqsManagedSseEnabled": "true", "RedrivePolicy": json.dumps({
                "deadLetterTargetArn": "arn:aws:sqs:us-east-1:123456789012:synthetic-dlq", "maxReceiveCount": 3})}}


def test_sqs_retry_does_not_ack_and_explicit_ack_deletes():
    client = FakeSQS()
    q = SQSQueue("https://sqs.example.test/queue", client=client)
    assert q.enqueue(event())
    delivery = q.claim()
    assert delivery.attempts == 2
    assert next(values for name, values in client.calls if name == 'receive')['WaitTimeSeconds'] == 20
    q.retry(delivery, delay=10, category="database_unavailable")
    assert not any(call[0] == "delete" for call in client.calls)
    q.ack(delivery)
    assert client.calls[-1][0] == "delete"


@pytest.mark.parametrize('wait', [-1, 21, True, 1.5, '20'])
def test_sqs_rejects_invalid_long_poll_wait(wait):
    with pytest.raises(ValueError, match='long-poll'):
        SQSQueue('https://sqs.example.test/queue', client=FakeSQS(), wait_seconds=wait)


def test_sqs_polling_client_timeout_exceeds_wait_without_slowing_control_calls(monkeypatch):
    import boto3
    clients, configs = [], []
    def create(service, *, config):
        assert service == 'sqs'
        clients.append(FakeSQS())
        configs.append(config)
        return clients[-1]
    monkeypatch.setattr(boto3, 'client', create)
    q = SQSQueue('https://sqs.example.test/queue', wait_seconds=20)
    assert configs[0].read_timeout == 5
    assert configs[1].read_timeout >= q.wait_seconds + 5
    assert configs[1].retries['total_max_attempts'] == 1
    assert q.claim() is None
    assert any(name == 'receive' and values['WaitTimeSeconds'] == 20 for name, values in clients[1].calls)
    assert not any(name == 'receive' for name, _ in clients[0].calls)


def test_sqs_zero_wait_is_explicit_and_uses_valid_poll_timeout(monkeypatch):
    client = FakeSQS()
    q = SQSQueue('https://sqs.example.test/queue', client=client, wait_seconds=0)
    assert q.claim() is None
    assert client.calls[-1][1]['WaitTimeSeconds'] == 0


def test_sqs_shutdown_does_not_wait_for_long_poll_or_query_network_depth(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    client = FakeSQS()
    def receive(**kwargs):
        entered.set()
        assert release.wait(2)
        return {}
    client.receive_message = receive
    q = SQSQueue('https://sqs.example.test/queue', client=client)
    monkeypatch.setenv('TRACE_SHUTDOWN_SECONDS', '0.05')
    store = TraceStore('unused', queue=q, workers=1, embedded=True)
    assert entered.wait(1)
    def must_not_query():
        raise AssertionError('shutdown must not query remote queue depth')
    q.stats = must_not_query
    start = time.monotonic()
    try:
        store.close()
        assert time.monotonic() - start < .5
        assert not any(name == 'delete' for name, _ in client.calls)
    finally:
        release.set()
        for worker in store._threads:
            worker.join(1)


@pytest.mark.parametrize("attributes, message", [
    ({"SqsManagedSseEnabled": "true"}, "dead-letter"),
    ({"SqsManagedSseEnabled": "true", "RedrivePolicy": "not json"}, "redrive"),
    ({"RedrivePolicy": json.dumps({"deadLetterTargetArn": "arn:synthetic", "maxReceiveCount": 3})}, "encryption"),
])
def test_sqs_requires_existing_encryption_and_dead_letter_policy(attributes, message):
    client = FakeSQS()
    client.get_queue_attributes = lambda **_: {"Attributes": attributes}
    with pytest.raises(ValueError, match=message):
        SQSQueue("https://sqs.example.test/queue", client=client)
    assert not any(name == "send" for name, _ in client.calls)


def test_sqs_approximate_capacity_rejects_before_send():
    client = FakeSQS()
    q = SQSQueue("https://sqs.example.test/queue", client=client, capacity=1)
    assert q.enqueue(event())
    assert not q.enqueue(event("overload"))
    assert len([call for call in client.calls if call[0] == "send"]) == 1
    assert q.stats()["counts_approximate"] is True
    assert q.stats()["rejected"] == 1


def test_sqs_consumer_deletes_only_after_database_commit(ingest_db):
    client = FakeSQS()
    q = SQSQueue("https://sqs.example.test/queue", client=client)
    store = TraceStore(ingest_db, queue=q, embedded=False)
    assert store.submit(event())
    original = client.delete_message
    def assert_committed(**kwargs):
        assert fetch(ingest_db, "SELECT count(*) AS n FROM traces")[0]["n"] == 1
        original(**kwargs)
    client.delete_message = assert_committed
    assert store.process_one()
    store.close()
