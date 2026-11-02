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
    monkeypatch.setenv("ALLOW_INSECURE_LOCAL", "true")
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
    assert not any("model_prices" in item.value for item in app.warning)


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


def test_authentication_fails_closed_before_database_access(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_LOCAL", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_FILE", raising=False)
    def unexpected_connection(*args, **kwargs):
        raise AssertionError("Unauthenticated dashboard must not access the database")
    monkeypatch.setattr(psycopg2, "connect", unexpected_connection)
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert "configure authentication" in app.error[0].value
    assert not app.metric


def test_dashboard_password_gate_denies_wrong_password(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_LOCAL", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "synthetic-operator-password-32-bytes-minimum")
    def unexpected_connection(*args, **kwargs):
        raise AssertionError("Bad password must not access the database")
    monkeypatch.setattr(psycopg2, "connect", unexpected_connection)
    app = AppTest.from_file(APP).run()
    app.text_input[0].set_value("wrong")
    next(x for x in app.button if x.label == "Sign in").click().run()
    assert not app.exception
    assert app.error[0].value == "Authentication failed."


def test_dashboard_password_allows_authenticated_access(monkeypatch, db, tmp_path):
    monkeypatch.delenv("ALLOW_INSECURE_LOCAL", raising=False)
    secret = tmp_path / "dashboard-password"
    secret.write_text("synthetic-file-password-32-bytes-minimum\n")
    monkeypatch.setenv("DASHBOARD_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("DATABASE_URL", db)
    app = AppTest.from_file(APP).run()
    app.text_input[0].set_value("synthetic-file-password-32-bytes-minimum")
    next(x for x in app.button if x.label == "Sign in").click().run()
    assert not app.exception
    assert any("No traces yet" in item.value for item in app.info)


def test_database_errors_do_not_disclose_details(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_LOCAL", "true")
    def unavailable(*args, **kwargs):
        raise psycopg2.OperationalError("synthetic-secret-password at private-host.internal")
    monkeypatch.setattr(psycopg2, "connect", unavailable)
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert "temporarily unavailable" in app.error[0].value
    assert "synthetic-secret-password" not in app.error[0].value


@pytest.mark.parametrize("secret", ["short", ""])
def test_dashboard_rejects_weak_configured_password_before_database(monkeypatch, secret):
    monkeypatch.delenv("ALLOW_INSECURE_LOCAL", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", secret)
    def unexpected_connection(*args, **kwargs):
        raise AssertionError("Weakly configured dashboard must not access the database")
    monkeypatch.setattr(psycopg2, "connect", unexpected_connection)
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert "configure authentication" in app.error[0].value
    assert not app.text_input


def test_password_rotation_revokes_existing_dashboard_session(monkeypatch, db, tmp_path):
    monkeypatch.delenv("ALLOW_INSECURE_LOCAL", raising=False)
    secret = tmp_path / "dashboard-password"
    secret.write_text("synthetic-password-before-rotation-32-bytes")
    monkeypatch.setenv("DASHBOARD_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("DATABASE_URL", db)
    app = AppTest.from_file(APP).run()
    app.text_input[0].set_value("synthetic-password-before-rotation-32-bytes")
    next(item for item in app.button if item.label == "Sign in").click().run()
    assert any("No traces yet" in item.value for item in app.info)
    secret.write_text("synthetic-password-after-rotation-32-bytes")
    def unexpected_connection(*args, **kwargs):
        raise AssertionError("Revoked session must not access the database")
    monkeypatch.setattr(psycopg2, "connect", unexpected_connection)
    app.run()
    assert not app.exception
    assert any(item.label == "Operator password" for item in app.text_input)
    assert not app.metric


def test_repeated_failed_login_attempts_are_throttled_within_session(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_LOCAL", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "synthetic-operator-password-32-bytes-minimum")
    def unexpected_connection(*args, **kwargs):
        raise AssertionError("Unauthenticated session must not access the database")
    monkeypatch.setattr(psycopg2, "connect", unexpected_connection)
    app = AppTest.from_file(APP).run()
    for _ in range(5):
        app.text_input[0].set_value("wrong")
        next(item for item in app.button if item.label == "Sign in").click().run()
    app.run()
    assert not app.exception
    assert "Too many failed attempts" in app.error[0].value
    assert not app.text_input


def test_missing_judge_usage_marks_cost_as_incomplete(monkeypatch, db):
    insert(db, [{"model": "gpt-4o-mini", "status": 200, "latency": 100, "pt": 20, "ct": 10,
                 "eval": "scored", "score": 1.0}])
    with contextlib.closing(psycopg2.connect(db)) as conn, conn, conn.cursor() as cur:
        cur.execute("UPDATE traces SET judge_model='gpt-4o-mini'")
    app = run_dashboard(monkeypatch, db)
    assert any("costs are incomplete" in item.value for item in app.warning)


def test_agent_filter_changes_metric_scope_using_bound_parameters(monkeypatch, db):
    insert(db, [{"model": "gpt-4o-mini", "status": 200, "latency": 100, "pt": 1, "ct": 1,
                 "eval": "scored", "score": 1.0}] * 2)
    with contextlib.closing(psycopg2.connect(db)) as conn, conn, conn.cursor() as cur:
        cur.execute("UPDATE traces SET agent_id=%s WHERE id=(SELECT min(id) FROM traces)", ("agent' OR 1=1 --",))
    app = run_dashboard(monkeypatch, db)
    next(x for x in app.selectbox if x.label == "Agent").set_value("agent' OR 1=1 --").run()
    assert not app.exception
    assert {m.label: m.value for m in app.metric}["Traces"] == "1"


def test_alert_acknowledgement_requires_explicit_capability(monkeypatch, db):
    with contextlib.closing(psycopg2.connect(db)) as conn, conn, conn.cursor() as cur:
        cur.execute("INSERT INTO alerts(metric,severity,message) VALUES ('quality','medium','synthetic')")
    monkeypatch.delenv("DASHBOARD_ALLOW_ALERT_ACK", raising=False)
    monkeypatch.delenv("DASHBOARD_OPERATOR_DATABASE_URL", raising=False)
    app = run_dashboard(monkeypatch, db)
    assert not any(item.label == "Acknowledge alert" for item in app.button)
    monkeypatch.setenv("DASHBOARD_ALLOW_ALERT_ACK", "true")
    monkeypatch.setenv("DASHBOARD_OPERATOR_DATABASE_URL", db)
    app = run_dashboard(monkeypatch, db)
    next(item for item in app.button if item.label == "Acknowledge alert").click().run()
    with contextlib.closing(psycopg2.connect(db)) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM alerts WHERE message='synthetic'")
        assert cur.fetchone()[0] == "acknowledged"
