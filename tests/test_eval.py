"""Eval worker tests against Postgres, with the mock upstream acting as the judge."""
import contextlib
import importlib
import sys

import psycopg2
import pytest
from psycopg2.extras import Json

from conftest import ROOT, fetch

sys.path.insert(0, str(ROOT / "eval"))


@pytest.fixture
def engine(monkeypatch, mock_upstream):
    monkeypatch.setenv("JUDGE_BASE_URL", f"{mock_upstream}/v1")
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("JUDGE_RETRY_BASE_SECONDS", "0")
    import eval_engine
    return importlib.reload(eval_engine)


@pytest.fixture
def judge(engine):
    # No SDK level retries, so each failed attempt is exactly one judge call.
    return engine.make_client().with_options(max_retries=0)


@pytest.fixture
def conn(db):
    with contextlib.closing(psycopg2.connect(db)) as connection:
        yield connection


def add_trace(dsn, *, user="How do I change my profile picture?", reply=None, status=200, tool_calls=None):
    message = {"role": "assistant", "content": reply}
    if tool_calls:
        message["tool_calls"] = tool_calls
    response = {"choices": [{"index": 0, "message": message}]} if status == 200 else {"error": {"message": "x"}}
    with contextlib.closing(psycopg2.connect(dsn)) as c, c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO traces (model, messages, response, status_code) VALUES (%s, %s, %s, %s) RETURNING id",
            ("gpt-4o-mini", Json([{"role": "system", "content": "Be brief."}, {"role": "user", "content": user}]),
             Json(response), status),
        )
        return cur.fetchone()[0]


def statuses(dsn) -> dict[int, dict]:
    return {r["id"]: r for r in fetch(dsn, "SELECT * FROM traces ORDER BY id")}


def test_every_trace_ends_scored_or_skipped(engine, judge, conn, db):
    good = add_trace(db, reply="Open Settings, choose Profile, then select Change picture.")
    off_topic = add_trace(db, reply="Your invoice has a $12.50 adjustment.")
    tool_only = add_trace(db, reply=None, tool_calls=[{"id": "c1", "type": "function",
                                                         "function": {"name": "lookup", "arguments": "{}"}}])
    error = add_trace(db, status=429)

    assert engine.run_once(conn, judge) == 4
    rows = statuses(db)
    assert rows[good]["eval_status"] == "scored" and float(rows[good]["relevance_score"]) == 1.0
    assert rows[off_topic]["eval_status"] == "scored" and float(rows[off_topic]["relevance_score"]) == 0.0
    assert rows[good]["judge_prompt_tokens"] > 0 and rows[good]["judge_model"] == engine.JUDGE_MODEL
    assert rows[tool_only]["eval_status"] == "skipped" and "tool call" in rows[tool_only]["eval_error"]
    assert rows[error]["eval_status"] == "skipped" and "429" in rows[error]["eval_error"]
    assert engine.run_once(conn, judge) == 0, "nothing should be judged twice"


def test_judge_failures_are_capped_and_do_not_block_the_queue(engine, judge, conn, db):
    failing = add_trace(db, reply="[judge-fail] this reply makes the mock judge return HTTP 500")
    behind = add_trace(db, reply="Open Settings, choose Profile, then select Change picture.")

    engine.run_once(conn, judge)
    rows = statuses(db)
    assert rows[failing]["eval_status"] == "failed"
    assert rows[failing]["eval_attempts"] == engine.MAX_ATTEMPTS
    assert "InternalServerError" in rows[failing]["eval_error"]
    assert rows[behind]["eval_status"] == "scored"


def test_retries_wait_for_backoff(engine, judge, conn, db, monkeypatch):
    monkeypatch.setattr(engine, "RETRY_BASE_SECONDS", 60)
    failing = add_trace(db, reply="[judge-fail] still failing")

    assert engine.run_once(conn, judge) == 1, "one attempt, then the row waits"
    row = statuses(db)[failing]
    assert row["eval_status"] == "pending" and row["eval_attempts"] == 1
    assert fetch(db, "SELECT eval_next_attempt_at > now() + interval '50 seconds' AS later FROM traces")[0]["later"]


def test_workers_never_claim_the_same_row(engine, judge, conn, db):
    first = add_trace(db, reply="Open Settings, choose Profile, then select Change picture.")
    second = add_trace(db, reply="Settings, then Profile, then Change picture.")
    with contextlib.closing(psycopg2.connect(db)) as other:
        with other.cursor() as cur:
            cur.execute(engine.CLAIM_SQL)  # worker A holds a lock on the first row
            assert cur.fetchone()[0] == first
            assert engine.process_one(conn, judge)  # worker B skips it
            other.rollback()
    rows = statuses(db)
    assert rows[first]["eval_status"] == "pending"
    assert rows[second]["eval_status"] == "scored"


@pytest.mark.parametrize("content, message", [
    ('{"score": 1.4, "reason": "x"}', "out of range"),
    ("not json", "Expecting value"),
    ('{"reason": "no score"}', "score"),
])
def test_bad_judge_output_is_rejected(engine, content, message):
    with pytest.raises((ValueError, KeyError), match=message):
        engine.parse_verdict(content)


def test_exchange_extraction_handles_content_parts(engine):
    messages = [{"role": "developer", "content": "Rules"},
                {"role": "user", "content": [{"type": "text", "text": "Where is my order?"}]}]
    response = {"choices": [{"message": {"content": "It ships today."}}]}
    assert engine.extract_exchange(messages, response) == ("Rules", "Where is my order?", "It ships today.")
    assert engine.extract_exchange(messages, {"choices": [{"message": {"content": "  "}}]}) is None
