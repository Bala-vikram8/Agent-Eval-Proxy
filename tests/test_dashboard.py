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


@pytest.mark.parametrize("model", ["gpt-4o-mini-2024-07-18"])
def test_dated_model_ids_use_the_family_price(monkeypatch, db, model):
    insert(db, [{"model": model, "status": 200, "latency": 900, "pt": 1_000_000, "ct": 1_000_000,
                 "eval": "pending", "score": None}])
    metrics = {m.label: m.value for m in run_dashboard(monkeypatch, db).metric}
    assert metrics["Model cost"] == "$0.7500"
