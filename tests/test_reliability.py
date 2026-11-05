"""Regression tests for defects found in the audit, and the durable ingestion guarantees.

Each test names the defect it pins down. Database outages are simulated with a TCP relay in
front of the real Postgres (conftest.TcpRelay), so the test server itself is never stopped."""
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import time

import httpx
import openai
import pytest

from conftest import ROOT, fetch, free_port, wait_for_traces, wait_until_up

TICKET = [{"role": "user", "content": "User cannot login, password reset email not arriving."}]


def client_for(url: str, key: str = "unused", **kw) -> openai.OpenAI:
    return openai.OpenAI(base_url=f"{url}/v1", api_key=key, max_retries=0, **kw)


def wait_for(predicate, timeout: float = 15, interval: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


# ---- model attribution and accounting --------------------------------------------------

def test_actual_model_is_recorded_and_priced(proxy, db):
    """Defect: only the requested alias was stored; the snapshot that served the call was lost."""
    reply = client_for(proxy).chat.completions.create(model="gpt-4o-mini", messages=TICKET)
    list(client_for(proxy).chat.completions.create(model="gpt-4o-mini", messages=TICKET, stream=True))
    rows = wait_for_traces(db, 2)
    assert reply.model == "gpt-4o-mini-2024-07-18"
    assert [(r["model"], r["response_model"]) for r in rows] == [("gpt-4o-mini", "gpt-4o-mini-2024-07-18")] * 2
    [summary] = fetch(db, "SELECT billed_model, unpriced, cost_usd FROM traces_enriched LIMIT 1")
    assert summary["billed_model"] == "gpt-4o-mini-2024-07-18" and summary["unpriced"] is False
    assert float(summary["cost_usd"]) == pytest.approx(
        (rows[0]["prompt_tokens"] * 0.15 + rows[0]["completion_tokens"] * 0.60) / 1e6)


def test_trace_id_header_matches_stored_trace(proxy, db):
    raw = client_for(proxy).chat.completions.with_raw_response.create(model="gpt-4o-mini", messages=TICKET)
    trace_id = raw.headers["x-agenteval-trace-id"]
    [row] = wait_for_traces(db, 1)
    assert row["trace_id"] == trace_id and row["ingested_at"] >= row["created_at"]


# ---- persistence defects ----------------------------------------------------------------

def test_nul_character_no_longer_drops_the_trace(proxy, db):
    """Defect: Postgres jsonb rejects U+0000 and the trace was silently dropped."""
    client_for(proxy).chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "login broken \u0000 please help"}])
    [row] = wait_for_traces(db, 1)
    assert row["messages"][0]["content"] == "login broken \ufffd please help"


def test_pii_is_redacted_before_storage_but_forwarded_unchanged(proxy, db, mock_upstream):
    content = "Patient SSN 123-45-6789, email pat@example.test, phone 555-555-0142, claim CLM-24-10031."
    client_for(proxy).chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": content}])
    [row] = wait_for_traces(db, 1)
    stored = row["messages"][0]["content"]
    assert "123-45-6789" not in stored and "pat@example.test" not in stored and "555-555-0142" not in stored
    assert "[REDACTED_SSN]" in stored and "CLM-24-10031" in stored
    assert row["redaction_count"] == 3


# ---- concurrency -------------------------------------------------------------------------

def test_concurrent_streams_are_reconstructed_independently(proxy, db):
    prompts = [f"Ticket {i}: {topic}" for i, topic in enumerate(
        ["login password reset"] * 5 + ["invoice billing"] * 5 + ["profile picture"] * 5 + ["general question"] * 5)]

    def run(prompt):
        stream = client_for(proxy).chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], stream=True)
        return prompt, "".join(c.choices[0].delta.content or "" for c in stream if c.choices)

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = dict(pool.map(run, prompts))
    rows = wait_for_traces(db, 20)
    assert len(rows) == 20 and len({r["trace_id"] for r in rows}) == 20
    for row in rows:
        prompt = row["messages"][0]["content"]
        assert row["response"]["choices"][0]["message"]["content"] == results[prompt]
        assert row["response"]["stream_completed"] is True and row["error_type"] is None


# ---- upstream failures -------------------------------------------------------------------

def test_upstream_timeout_is_504_and_classified(start_proxy, database, mock_upstream, db):
    """Defect: a timeout was reported as a generic 502."""
    url = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=mock_upstream, UPSTREAM_API_KEY="k",
                      UPSTREAM_TIMEOUT_SECONDS="0.5")
    response = httpx.post(f"{url}/v1/chat/completions", timeout=10, json={
        "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "slow [mock-sleep:2]"}]})
    assert response.status_code == 504
    [row] = wait_for_traces(db, 1)
    assert row["status_code"] == 504 and row["error_type"] == "upstream_timeout"
    assert row["trace_id"] == response.headers["x-agenteval-trace-id"]


def test_mid_stream_upstream_failure_is_visible_to_caller_and_trace(proxy, db):
    """Defect: a cut upstream stream looked like a short successful answer."""
    stream = client_for(proxy).chat.completions.create(
        model="gpt-4o-mini", stream=True, messages=[{"role": "user", "content": "explain [mock-drop-stream]"}])
    with pytest.raises(openai.APIError, match="Upstream stream failed"):
        list(stream)
    [row] = wait_for_traces(db, 1)
    assert row["status_code"] == 200 and row["error_type"].endswith("_mid_stream")
    assert row["response"]["choices"][0]["message"]["content"] == "Partial answer "
    [summary] = fetch(db, "SELECT errors FROM trace_summary")
    assert summary["errors"] == 1, "a broken stream counts as a failure"


def test_client_disconnect_is_classified_and_not_an_error(proxy, db):
    body = {"model": "gpt-4o-mini", "messages": TICKET, "stream": True}
    with httpx.stream("POST", f"{proxy}/v1/chat/completions", json=body, timeout=30) as response:
        next(response.iter_raw())
    [row] = wait_for_traces(db, 1)
    assert row["error_type"] == "client_disconnect"
    [summary] = fetch(db, "SELECT errors, client_disconnects, p50_latency_ms FROM trace_summary")
    assert (summary["errors"], summary["client_disconnects"], summary["p50_latency_ms"]) == (0, 1, None)


def test_unrecorded_sse_endpoints_are_relayed_incrementally(proxy, db):
    """Defect: non chat endpoints were buffered with aread(), so a streamed Responses call
    arrived all at once at the end."""
    arrivals = []
    start = time.perf_counter()
    with httpx.stream("POST", f"{proxy}/v1/responses", json={"model": "gpt-4o-mini", "stream": True}, timeout=30) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                arrivals.append(time.perf_counter() - start)
    assert len(arrivals) == 5
    assert arrivals[-1] - arrivals[0] > 0.3, "the mock waits 100 ms between events"
    time.sleep(0.3)
    assert fetch(db, "SELECT count(*) AS n FROM traces")[0]["n"] == 0


# ---- input validation and proxy-only headers ------------------------------------------------

def test_agenteval_headers_are_consumed_not_forwarded(proxy, db, mock_upstream):
    client_for(proxy).chat.completions.create(
        model="gpt-4o-mini", messages=TICKET,
        extra_headers={"X-AgentEval-Case-Id": "CLM-24-10031", "X-AgentEval-Evaluations": "grounded"})
    [row] = wait_for_traces(db, 1)
    assert row["case_ref"] == "CLM-24-10031" and row["grounded_eval"] is True
    forwarded = httpx.get(f"{mock_upstream}/mock/seen-headers").json()["headers"][-1]
    assert not [h for h in forwarded if h.startswith("x-agenteval")]


@pytest.mark.parametrize("headers, message", [
    ({"X-AgentEval-Case-Id": "x' OR 1=1 --"}, "Case-Id"),
    ({"X-AgentEval-Case-Id": "a" * 65}, "Case-Id"),
    ({"X-AgentEval-Evaluations": "grounded,telepathy"}, "telepathy"),
])
def test_invalid_agenteval_headers_are_rejected(proxy, db, headers, message):
    response = httpx.post(f"{proxy}/v1/chat/completions", headers=headers,
                          json={"model": "gpt-4o-mini", "messages": TICKET})
    assert response.status_code == 400 and message in response.json()["error"]["message"]
    time.sleep(0.2)
    assert fetch(db, "SELECT count(*) AS n FROM traces")[0]["n"] == 0


def test_oversized_body_is_rejected(start_proxy, database, mock_upstream):
    url = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=mock_upstream, UPSTREAM_API_KEY="k",
                      MAX_REQUEST_BYTES="1000")
    response = httpx.post(f"{url}/v1/chat/completions",
                          json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x" * 2000}]})
    assert response.status_code == 413


def test_metrics_require_the_proxy_key_when_one_is_set(start_proxy, database, mock_upstream):
    url = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=mock_upstream, UPSTREAM_API_KEY="k",
                      PROXY_API_KEY="team-secret")
    assert httpx.get(f"{url}/metrics").status_code == 401
    metrics = httpx.get(f"{url}/metrics", headers={"authorization": "Bearer team-secret"})
    assert metrics.status_code == 200 and "agenteval_trace_spool_depth" in metrics.text
    assert httpx.get(f"{url}/health/ready").status_code == 200, "probes stay unauthenticated"


# ---- health -------------------------------------------------------------------------------

def test_readiness_reports_database_outage_and_full_spool(start_proxy, mock_upstream):
    """Defect: /health said ok with the database down and there was no readiness signal."""
    url = start_proxy(DATABASE_URL=f"postgresql://nobody@127.0.0.1:{free_port()}/none",
                      UPSTREAM_BASE=mock_upstream, UPSTREAM_API_KEY="k", TRACE_SPOOL_MAX_ROWS="2")
    client_for(url).chat.completions.create(model="gpt-4o-mini", messages=TICKET)
    ready = wait_for(lambda: (r := httpx.get(f"{url}/health/ready")).json()["trace_store"]["postgres_ok"] is False and r)
    assert ready.status_code == 200 and ready.json()["status"].startswith("degraded")
    assert httpx.get(f"{url}/health/live").json() == {"status": "ok"}

    for _ in range(2):
        client_for(url).chat.completions.create(model="gpt-4o-mini", messages=TICKET)
    ready = httpx.get(f"{url}/health/ready")
    assert ready.status_code == 503 and ready.json()["trace_store"]["dropped"] == 1
    assert "agenteval_traces_dropped_total 1" in httpx.get(f"{url}/metrics").text


# ---- durable ingestion ------------------------------------------------------------------------

def test_traces_survive_a_database_outage_and_arrive_after_recovery(start_proxy, pg_relay, mock_upstream, db):
    relay, dsn = pg_relay
    url = start_proxy(DATABASE_URL=dsn, UPSTREAM_BASE=mock_upstream, UPSTREAM_API_KEY="k")
    client_for(url).chat.completions.create(model="gpt-4o-mini", messages=TICKET)
    assert len(wait_for_traces(db, 1)) == 1

    relay.stop()
    for _ in range(5):
        assert client_for(url).chat.completions.create(model="gpt-4o-mini", messages=TICKET).choices
    stats = wait_for(lambda: (s := httpx.get(f"{url}/health/ready").json()["trace_store"])["postgres_ok"] is False and s)
    assert stats["spool_depth"] == 5
    time.sleep(1)
    assert fetch(db, "SELECT count(*) AS n FROM traces")[0]["n"] == 1, "nothing written while the database is away"

    relay.start()
    rows = wait_for_traces(db, 6, timeout=45)
    assert len(rows) == 6 and len({r["trace_id"] for r in rows}) == 6
    delayed = [r for r in rows[1:]]
    assert all((r["ingested_at"] - r["created_at"]).total_seconds() > 0.5 for r in delayed), \
        "created_at keeps the request time, ingested_at shows the delay"
    assert wait_for(lambda: httpx.get(f"{url}/health/ready").json()["trace_store"]["spool_depth"] == 0)


def start_proxy_process(env: dict, port: int) -> subprocess.Popen:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("UPSTREAM", "PROXY", "OPENAI", "TRACE"))}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT / "proxy", env={**base, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_until_up(f"http://127.0.0.1:{port}/health", proc)
    return proc


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM])
def test_spooled_traces_survive_process_termination(sig, pg_relay, mock_upstream, db, tmp_path):
    """Traces accepted while the database is unreachable survive kill -9 (and a normal stop
    that cannot drain) and are written exactly once by the next process."""
    relay, dsn = pg_relay
    env = {"DATABASE_URL": dsn, "UPSTREAM_BASE": mock_upstream, "UPSTREAM_API_KEY": "k",
           "TRACE_SPOOL_PATH": str(tmp_path / "spool.db")}
    relay.stop()
    port = free_port()
    proc = start_proxy_process(env, port)
    try:
        trace_ids = []
        for _ in range(3):
            raw = client_for(f"http://127.0.0.1:{port}").chat.completions.with_raw_response.create(
                model="gpt-4o-mini", messages=TICKET)
            trace_ids.append(raw.headers["x-agenteval-trace-id"])
        proc.send_signal(sig)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert fetch(db, "SELECT count(*) AS n FROM traces")[0]["n"] == 0

    relay.start()
    proc = start_proxy_process(env, port)
    try:
        rows = wait_for_traces(db, 3, timeout=30)
        assert sorted(r["trace_id"] for r in rows) == sorted(trace_ids)
        ready = httpx.get(f"http://127.0.0.1:{port}/health/ready").json()["trace_store"]
        assert ready["recovered_at_start"] == 3
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    assert len(fetch(db, "SELECT id FROM traces")) == 3, "no duplicates after restart"


# ---- TraceStore unit level --------------------------------------------------------------------

sys.path.insert(0, str(ROOT / "proxy"))
from store import TraceStore  # noqa: E402


def record(trace_id: str, **extra) -> dict:
    return {"trace_id": trace_id, "model": "gpt-4o-mini", "messages": [], "response": {}, "status_code": 200,
            "streamed": False, "created_at": time.time(), **extra}


def test_redelivery_is_idempotent(db, tmp_path):
    """A crash between the Postgres commit and the spool delete redelivers the batch."""
    store = TraceStore(db, str(tmp_path / "s.db"))
    store.submit(record("dup-1"))
    store.drain_once()
    store.submit(record("dup-1"))  # same trace id again, as after a crash
    store.drain_once()
    store.close()
    assert fetch(db, "SELECT count(*) AS n FROM traces WHERE trace_id = 'dup-1'")[0]["n"] == 1


def test_rejected_row_is_dead_lettered_without_blocking_others(db, tmp_path):
    store = TraceStore(db, str(tmp_path / "s.db"))
    store.submit(record("good-1"))
    store.submit(record("bad-1", status_code="not a number"))
    store.submit(record("good-2"))
    for _ in range(3):
        store.drain_once()
    stats = store.stats()
    store.close()
    assert {r["trace_id"] for r in fetch(db, "SELECT trace_id FROM traces")} == {"good-1", "good-2"}
    assert stats["dead_lettered"] == 1 and stats["dead_letter_rows"] == 1 and stats["spool_depth"] == 0


def test_backoff_is_bounded_and_does_not_spin(tmp_path):
    store = TraceStore(f"postgresql://nobody@127.0.0.1:{free_port()}/none", str(tmp_path / "s.db"),
                       retry_base=0.05, retry_max=0.2)
    store.submit(record("t-1"))
    store.start()
    time.sleep(1.5)
    failures = store.stats()["drain_failures"]
    store.close(drain_timeout=0.1)
    # Doubling from 50 ms capped at 200 ms gives about 9 attempts in 1.5 s; a busy loop gives thousands.
    assert 3 <= failures <= 15
    reopened = TraceStore("postgresql://unused", str(tmp_path / "s.db"))
    assert reopened.depth == 1, "the undelivered trace is still on disk"
    reopened.close(drain_timeout=0)


def test_stored_payload_never_contains_raw_pii(tmp_path):
    """The spool is disk too, so redaction has to happen before submit(); check the file."""
    import sqlite3
    from redaction import redact
    messages, count = redact([{"role": "user", "content": "SSN 123-45-6789, card 4111 1111 1111 1111"}])
    assert count == 2
    store = TraceStore("postgresql://unused", str(tmp_path / "s.db"))
    store.submit(record("p-1", messages=messages))
    payload = sqlite3.connect(tmp_path / "s.db").execute("SELECT payload FROM spool").fetchone()[0]
    store.close(drain_timeout=0)
    assert "123-45-6789" not in payload and "4111" not in payload
    assert json.loads(payload)["messages"][0]["content"] == "SSN [REDACTED_SSN], card [REDACTED_CARD]"
