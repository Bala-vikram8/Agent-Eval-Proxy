"""The schema file must be safe to rerun and must upgrade databases from earlier versions."""
import contextlib
import sys
import uuid

import psycopg2
from psycopg2.extras import Json

from conftest import ROOT, apply_schema, fetch

sys.path.insert(0, str(ROOT / "proxy"))
from store import TraceStore  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def execute(dsn, query, params=None):
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn, conn.cursor() as cur:
        cur.execute(query, params)


def columns(dsn) -> set[str]:
    rows = fetch(dsn, "SELECT column_name FROM information_schema.columns WHERE table_name = 'traces'")
    return {r["column_name"] for r in rows}


def proxy_can_write(dsn) -> bool:
    """Insert one trace exactly the way the proxy does."""
    marker = uuid.uuid4().hex
    store = TraceStore(dsn, workers=1)
    store.submit({"request_id": marker, "model": "gpt-4o-mini", "messages": [], "response": {},
                  "status_code": 200, "streamed": False})
    store.close()  # waits for the queued write
    return fetch(dsn, "SELECT count(*) AS n FROM traces WHERE request_id = %s", (marker,))[0]["n"] == 1


def test_fresh_database_and_rerun(make_database):
    dsn = make_database()
    apply_schema(dsn)
    apply_schema(dsn)
    assert {"eval_status", "relevance_score", "ttft_ms", "judge_model"} <= columns(dsn)
    assert proxy_can_write(dsn)


def test_upgrade_from_first_version(make_database):
    dsn = make_database()
    apply_schema(dsn, FIXTURES / "schema_v1.sql")
    execute(dsn, """INSERT INTO traces (model, messages, response, prompt_tokens, completion_tokens, latency_ms, cost_usd)
                    VALUES ('gpt-4o-mini', %s, %s, 47, 55, 1397, 0.00004)""",
            (Json([{"role": "user", "content": "hi"}]), Json({"choices": [{"message": {"content": "hello"}}]})))
    apply_schema(dsn)
    apply_schema(dsn)

    assert "cost_usd" not in columns(dsn)
    [row] = fetch(dsn, "SELECT status_code, eval_status, cost_usd FROM traces_enriched WHERE messages <> '[]'::jsonb")
    assert row["status_code"] == 200 and row["eval_status"] == "pending"
    assert float(row["cost_usd"]) == (47 * 0.15 + 55 * 0.60) / 1_000_000
    assert proxy_can_write(dsn)


def test_upgrade_from_second_version_keeps_scores(make_database):
    dsn = make_database()
    apply_schema(dsn, FIXTURES / "schema_v2.sql")
    execute(dsn, """INSERT INTO traces (model, messages, response, status_code, prompt_tokens, completion_tokens,
                                        faithfulness_score, faithfulness_reason)
                    VALUES ('gpt-5.6-terra', '[]', '{}', 200, 40, 30, 1.0, 'relevant')""")
    apply_schema(dsn)

    assert not {"faithfulness_score", "faithfulness_reason", "tool_correct"} & columns(dsn)
    [row] = fetch(dsn, "SELECT eval_status, relevance_score, relevance_reason, unpriced FROM traces_enriched")
    assert row["eval_status"] == "scored" and float(row["relevance_score"]) == 1.0
    assert row["relevance_reason"] == "relevant" and row["unpriced"] is True
    assert proxy_can_write(dsn)
