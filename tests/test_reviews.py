"""Human review state machine: allowed transitions, duplicate protection, audit trail."""
import contextlib
import sys
import threading

import psycopg2
import pytest
from psycopg2.extras import Json

from conftest import ROOT, fetch

sys.path.insert(0, str(ROOT / "dashboard"))
import reviews  # noqa: E402


def make_review(dsn, case_ref="CLM-24-10047") -> int:
    with contextlib.closing(psycopg2.connect(dsn)) as c, c, c.cursor() as cur:
        cur.execute("INSERT INTO traces (model, status_code, case_ref) VALUES ('m', 200, %s) RETURNING id", (case_ref,))
        trace = cur.fetchone()[0]
        cur.execute("""INSERT INTO trace_evaluations (trace_id, eval_type, status, verdict, needs_review)
                       VALUES (%s, 'grounded', 'completed', 'unsupported', true) RETURNING id""", (trace,))
        evaluation = cur.fetchone()[0]
        cur.execute("""INSERT INTO review_items (trace_id, evaluation_id, case_ref, reason, findings)
                       VALUES (%s, %s, %s, 'unsupported: $430.00', %s) RETURNING id""",
                    (trace, evaluation, case_ref, Json({"verdict": "unsupported"})))
        return cur.fetchone()[0]


@pytest.fixture
def conn(db):
    with contextlib.closing(psycopg2.connect(db)) as connection:
        yield connection


def test_full_lifecycle_is_recorded(conn, db):
    review = make_review(db)
    item = reviews.transition(conn, review, to_status="rejected", actor="A. Reviewer", expected_status="pending",
                              expected_version=1, note="Policy v4 (180 days) is retired; v5 allows 90 days.")
    assert (item.status, item.version) == ("rejected", 2)
    item = reviews.transition(conn, review, to_status="resolved", actor="B. Lead", expected_status="rejected",
                              expected_version=2, note="Routed to write-off review.")
    assert item.status == "resolved"

    [row] = fetch(db, "SELECT * FROM review_items")
    assert row["decided_by"] == "A. Reviewer" and row["decided_at"] is not None
    assert row["resolved_by"] == "B. Lead" and row["resolved_at"] >= row["decided_at"]
    assert [(h["from_status"], h["to_status"], h["actor"]) for h in reviews.history(conn, review)] == [
        ("pending", "rejected", "A. Reviewer"), ("rejected", "resolved", "B. Lead")]


@pytest.mark.parametrize("kwargs, message", [
    ({"to_status": "resolved", "expected_status": "pending"}, "cannot move from pending to resolved"),
    ({"to_status": "pending", "expected_status": "approved"}, "cannot move"),
    ({"to_status": "rejected", "expected_status": "pending"}, "needs a note"),
    ({"to_status": "approved", "expected_status": "pending", "actor": "  "}, "reviewer name is required"),
])
def test_invalid_actions_are_refused_and_change_nothing(conn, db, kwargs, message):
    review = make_review(db)
    with pytest.raises(reviews.ReviewError, match=message):
        reviews.transition(conn, review, **{"actor": "A", "expected_version": 1, **kwargs})
    [row] = fetch(db, "SELECT status, version FROM review_items")
    assert (row["status"], row["version"]) == ("pending", 1)
    assert reviews.history(conn, review) == []


def test_a_repeated_click_is_not_applied_twice(conn, db):
    review = make_review(db)
    reviews.transition(conn, review, to_status="approved", actor="A", expected_status="pending", expected_version=1)
    with pytest.raises(reviews.ReviewError, match="changed since it was loaded"):
        reviews.transition(conn, review, to_status="approved", actor="A", expected_status="pending", expected_version=1)
    assert len(reviews.history(conn, review)) == 1


def test_two_reviewers_acting_at_once_produce_one_decision(db):
    review = make_review(db)
    outcomes = []

    def act(to_status):
        with contextlib.closing(psycopg2.connect(db)) as c:
            try:
                reviews.transition(c, review, to_status=to_status, actor=to_status, expected_status="pending",
                                   expected_version=1, note="n")
                outcomes.append(to_status)
            except reviews.ReviewError:
                outcomes.append("refused")

    threads = [threading.Thread(target=act, args=(s,)) for s in ("approved", "rejected") * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("refused") == 7
    assert len(fetch(db, "SELECT id FROM review_events")) == 1


def test_audit_trail_is_append_only(conn, db):
    review = make_review(db)
    reviews.transition(conn, review, to_status="approved", actor="A", expected_status="pending", expected_version=1)
    with pytest.raises(psycopg2.Error, match="append only"):
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE review_events SET actor = 'someone else'")
    assert reviews.history(conn, review)[0]["actor"] == "A"


def test_unknown_review(conn, db):
    with pytest.raises(reviews.ReviewError, match="does not exist"):
        reviews.transition(conn, 999, to_status="approved", actor="A", expected_status="pending", expected_version=1)
