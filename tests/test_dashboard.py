"""Renders the Streamlit dashboard headlessly against real views."""
import contextlib

import psycopg2
import pytest
from psycopg2.extras import Json
from streamlit.testing.v1 import AppTest

from conftest import ROOT

APP = str(ROOT / "dashboard" / "app.py")


def run_dashboard(monkeypatch, dsn) -> AppTest:
    monkeypatch.setenv("DATABASE_URL", dsn)
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception, app.exception
    return app


def insert(dsn, rows):
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn, conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """INSERT INTO traces (model, status_code, latency_ms, prompt_tokens, completion_tokens,
                   eval_status, relevance_score, relevance_reason, messages, response)
                   VALUES (%(model)s, %(status)s, %(latency)s, %(pt)s, %(ct)s, %(eval)s, %(score)s, 'r',
                           %(messages)s, %(response)s)""",
                {"messages": Json([{"role": "user", "content": "q"}]),
                 "response": Json({"choices": [{"message": {"content": "a"}}]}), **row},
            )


def test_empty_database_shows_hint(monkeypatch, db):
    app = run_dashboard(monkeypatch, db)
    assert "No traces yet" in app.info[0].value


def test_metrics_cover_all_traces_not_just_recent(monkeypatch, db):
    rows = [{"model": "gpt-4o-mini", "status": 200, "latency": 100 + i, "pt": 1_000_000, "ct": 0,
             "eval": "scored", "score": 1.0} for i in range(60)]
    rows.append({"model": "gpt-4o-mini", "status": 429, "latency": 5, "pt": None, "ct": None,
                 "eval": "skipped", "score": None})
    insert(db, rows)
    app = run_dashboard(monkeypatch, db)
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Traces"] == "61"          # the old dashboard capped this at 20
    assert metrics["Error rate"] == "1.6%"
    assert metrics["Model cost"] == "$9.0000"  # 60 calls x 1M input tokens x $0.15
    assert metrics["Scored / skipped"] == "60 / 1"
    assert not app.warning


def test_unpriced_models_are_called_out(monkeypatch, db):
    insert(db, [{"model": "gpt-5.6-terra", "status": 200, "latency": 900, "pt": 40, "ct": 30,
                 "eval": "scored", "score": 0.2}])
    app = run_dashboard(monkeypatch, db)
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Model cost"] == "$0.000000"
    assert "no row in model_prices" in app.warning[0].value
    assert any("score 0.20" in e.label for e in app.expander)


def test_metrics_match_an_independent_computation(monkeypatch, db):
    """Every headline number is recomputed here in Python from raw rows and compared."""
    rows = []
    for i in range(30):
        rows.append({"model": "gpt-4o-mini", "status": 200, "latency": 100 + 10 * i, "pt": 1000, "ct": 500,
                     "eval": "scored", "score": 0.5 if i % 2 else 1.0})
    rows.append({"model": "gpt-4o-mini", "status": 502, "latency": 7, "pt": None, "ct": None, "eval": "skipped",
                 "score": None})
    insert(db, rows)
    with contextlib.closing(psycopg2.connect(db)) as conn, conn, conn.cursor() as cur:
        # One stream cut by the upstream (a failure) and one abandoned by the caller (not a failure).
        cur.execute("""INSERT INTO traces (model, status_code, latency_ms, streamed, error_type, eval_status)
                       VALUES ('gpt-4o-mini', 200, 9000, true, 'upstream_error_mid_stream', 'skipped'),
                              ('gpt-4o-mini', 200, 8000, true, 'client_disconnect', 'skipped')""")
    metrics = {m.label: m.value for m in run_dashboard(monkeypatch, db).metric}

    completed = sorted(100 + 10 * i for i in range(30))
    p50 = (completed[14] + completed[15]) / 2
    assert metrics["Traces"] == "33"
    assert metrics["Error rate"] == f"{100 * 2 / 33:.1f}%"          # the 502 and the cut stream
    assert metrics["p50 latency"] == f"{p50:,.0f} ms"                # interrupted streams excluded
    assert metrics["Model cost"] == f"${30 * (1000 * 0.15 + 500 * 0.60) / 1e6:,.4f}"  # $0.0135
    assert metrics["Avg relevance"] == "0.75"
    assert metrics["Client disconnects"] == "1"


def test_review_queue_renders_and_records_a_decision(monkeypatch, db):
    insert(db, [{"model": "gpt-4o-mini", "status": 200, "latency": 10, "pt": 1, "ct": 1, "eval": "scored", "score": 1}])
    with contextlib.closing(psycopg2.connect(db)) as conn, conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO trace_evaluations (trace_id, eval_type, status, verdict, needs_review)
                       VALUES (1, 'grounded', 'completed', 'unsupported', true) RETURNING id""")
        cur.execute("""INSERT INTO review_items (trace_id, evaluation_id, case_ref, reason, findings)
                       VALUES (1, %s, 'CLM-24-10047', 'unsupported', %s)""",
                    (cur.fetchone()[0], Json({"verdict": "unsupported", "reasons": ["identifiers not found: $430.00"],
                                              "request": "q", "reply": "pay $430.00",
                                              "evidence": [{"id": "E1", "source": "get_claim", "text": "{}"}]})))
    app = run_dashboard(monkeypatch, db)
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Reviews pending"] == "1"
    assert any("Review #1 [pending] CLM-24-10047: unsupported" in e.label for e in app.expander)
    assert any("does not block, change or recall" in c.value for c in app.caption)

    app.text_input(key="reviewer").input("Dana Reviewer")
    app.radio(key="action-1-1").set_value("approved")
    app.button[0].click().run()
    assert not app.exception
    [row] = fetch_review(db)
    assert (row[0], row[1]) == ("approved", "Dana Reviewer")


def fetch_review(dsn):
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, decided_by FROM review_items")
        return cur.fetchall()


def test_password_gate(monkeypatch, db):
    insert(db, [{"model": "gpt-4o-mini", "status": 200, "latency": 10, "pt": 1, "ct": 1, "eval": "scored", "score": 1}])
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct horse")
    app = run_dashboard(monkeypatch, db)
    assert not app.metric, "nothing is shown before the password"
    app.text_input[0].input("wrong").run()
    assert app.error[0].value == "Wrong password." and not app.metric
    app.text_input[0].input("correct horse").run()
    assert {m.label for m in app.metric} >= {"Traces"}


@pytest.mark.parametrize("model", ["gpt-4o-mini-2024-07-18"])
def test_dated_model_ids_use_the_family_price(monkeypatch, db, model):
    insert(db, [{"model": model, "status": 200, "latency": 900, "pt": 1_000_000, "ct": 1_000_000,
                 "eval": "pending", "score": None}])
    metrics = {m.label: m.value for m in run_dashboard(monkeypatch, db).metric}
    assert metrics["Model cost"] == "$0.7500"
