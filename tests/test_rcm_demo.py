"""End to end run of the synthetic denial demo: LangGraph agent -> proxy -> mock model
(scripted from demos/rcm_denial/offline_script.json) -> spool -> Postgres -> eval worker with
the mock judge -> review items -> review CLI.

The model's recommendations are scripted in offline mode. The verdicts are not: they come from
grounded.evaluate() on whatever the proxy stored."""
import contextlib
import importlib
import json
import os
import subprocess
import sys

import psycopg2
import pytest

from conftest import ROOT, fetch, free_port, run_server, wait_for_traces

DEMO = ROOT / "demos" / "rcm_denial"
EXPECTED = {"CLM-24-10031": "supported", "CLM-24-10047": "unsupported", "CLM-24-10052": "insufficient_evidence"}


@pytest.fixture(scope="module")
def scripted_upstream():
    env = {"MOCK_SCRIPT_PATH": str(DEMO / "offline_script.json")}
    with run_server("mock_upstream", "server:app", free_port(), env, "/v1/models") as url:
        yield url


def test_denial_demo_end_to_end(start_proxy, database, scripted_upstream, db, monkeypatch):
    proxy = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=scripted_upstream, UPSTREAM_API_KEY="offline")
    monkeypatch.setenv("PROXY_URL", f"{proxy}/v1")
    sys.path.insert(0, str(DEMO))
    agent = importlib.reload(importlib.import_module("agent"))

    finals = {}
    for claim_id in EXPECTED:
        state = agent.investigate(claim_id)
        assert len(state["trace_ids"]) == 2, "one tool call turn, one recommendation"
        finals[claim_id] = state["trace_ids"][-1]
    wait_for_traces(db, 6)

    monkeypatch.setenv("JUDGE_BASE_URL", f"{scripted_upstream}/v1")
    monkeypatch.setenv("JUDGE_API_KEY", "offline")
    sys.path.insert(0, str(ROOT / "eval"))
    engine = importlib.reload(importlib.import_module("eval_engine"))
    with contextlib.closing(psycopg2.connect(db)) as conn:
        engine.run_once(conn, engine.make_client())

    rows = fetch(db, """
        SELECT t.case_ref, t.trace_id, t.redaction_count, e.status, e.verdict, e.needs_review, e.result, r.id AS review_id
        FROM traces t JOIN trace_evaluations e ON e.trace_id = t.id
        LEFT JOIN review_items r ON r.evaluation_id = e.id
        WHERE t.trace_id = ANY(%s)""", (list(finals.values()),))
    by_case = {r["case_ref"]: r for r in rows}
    assert {c: r["verdict"] for c, r in by_case.items()} == EXPECTED
    assert by_case["CLM-24-10031"]["review_id"] is None
    assert by_case["CLM-24-10047"]["result"]["layers"]["identifier_check"]["unsupported_identifiers"] == ["$430.00"]
    assert by_case["CLM-24-10052"]["review_id"] is not None
    # Synthetic patient contact details in tool results are redacted before storage.
    assert all(r["redaction_count"] >= 2 for r in rows)
    stored = json.dumps(fetch(db, "SELECT messages FROM traces")[-1]["messages"])
    assert "example.test" not in stored and "555-555-01" not in stored

    tool_turns = fetch(db, """SELECT e.status, e.error FROM trace_evaluations e JOIN traces t ON t.id = e.trace_id
                              WHERE t.trace_id <> ALL(%s)""", (list(finals.values()),))
    assert len(tool_turns) == 3 and all(t["status"] == "skipped" for t in tool_turns)

    review_id = by_case["CLM-24-10047"]["review_id"]
    cli = [sys.executable, str(ROOT / "dashboard" / "review_cli.py")]
    env = {**os.environ, "DATABASE_URL": db}
    reject = subprocess.run(cli + ["decide", str(review_id), "rejected", "--actor", "Demo Reviewer", "--note",
                                   "Policy version 4 (180 days) was retired; version 5 allows 90 days. No $430.00 in evidence."],
                            env=env, capture_output=True, text=True)
    assert reject.returncode == 0, reject.stderr
    again = subprocess.run(cli + ["decide", str(review_id), "rejected", "--actor", "Demo Reviewer", "--note", "x"],
                           env=env, capture_output=True, text=True)
    assert again.returncode == 1 and "cannot move from rejected to rejected" in again.stderr
    resolve = subprocess.run(cli + ["decide", str(review_id), "resolved", "--actor", "Demo Lead"],
                             env=env, capture_output=True, text=True)
    assert resolve.returncode == 0, resolve.stderr
    events = fetch(db, "SELECT to_status, actor FROM review_events WHERE review_id = %s ORDER BY id", (review_id,))
    assert [(e["to_status"], e["actor"]) for e in events] == [
        ("pending", "system:eval-worker"), ("rejected", "Demo Reviewer"), ("resolved", "Demo Lead")]
