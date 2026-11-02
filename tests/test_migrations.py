"""The schema file must be safe to rerun and must upgrade databases from earlier versions."""
import contextlib
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

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


def test_upgrade_preserves_version_one_evaluation_history(make_database):
    dsn = make_database()
    apply_schema(dsn, FIXTURES / "schema_v1.sql")
    execute(dsn, "INSERT INTO traces(id,model,messages,response) VALUES (71,'synthetic','[]','{}')")
    execute(dsn, """INSERT INTO evaluations(trace_id,faithfulness_score,judge_reason)
        VALUES (71,0.75,'legacy synthetic relevance')""")
    apply_schema(dsn)
    apply_schema(dsn)
    [row] = fetch(dsn, "SELECT eval_status,relevance_score,relevance_reason FROM traces WHERE id=71")
    assert row["eval_status"] == "scored" and float(row["relevance_score"]) == 0.75
    assert row["relevance_reason"] == "legacy synthetic relevance"
    runs = fetch(dsn, "SELECT * FROM evaluation_runs WHERE trace_id=71 AND config_version='schema-v1'")
    assert len(runs) == 1 and runs[0]["evaluator_name"] == "answer_relevance"


def test_exact_dated_model_price_takes_precedence_and_missing_judge_is_visible(make_database):
    dsn = make_database()
    apply_schema(dsn)
    execute(dsn, """INSERT INTO model_prices(model,input_usd_per_mtok,output_usd_per_mtok)
        VALUES ('gpt-4o-mini-2026-11-01',2,4)""")
    execute(dsn, """INSERT INTO traces(model,status_code,prompt_tokens,completion_tokens,
        judge_model,judge_prompt_tokens,judge_completion_tokens)
        VALUES ('gpt-4o-mini-2026-11-01',200,1000,1000,'missing-judge',10,10)""")
    [row] = fetch(dsn, "SELECT cost_usd,judge_cost_usd,judge_unpriced,usage_missing FROM traces_enriched")
    assert float(row["cost_usd"]) == 0.006
    assert row["judge_cost_usd"] is None and row["judge_unpriced"] and not row["usage_missing"]
    [summary] = fetch(dsn, "SELECT unpriced_judge_traces,missing_usage_traces FROM trace_summary")
    assert summary["unpriced_judge_traces"] == 1 and summary["missing_usage_traces"] == 0


def test_unknown_judge_usage_is_explicit_in_cost_views(make_database):
    dsn = make_database()
    apply_schema(dsn)
    execute(dsn, """INSERT INTO traces(model,status_code,judge_model,eval_status)
        VALUES ('gpt-4o-mini',200,'gpt-4o-mini','failed')""")
    [row] = fetch(dsn, "SELECT judge_cost_usd,judge_usage_missing FROM traces_enriched")
    assert row["judge_cost_usd"] is None and row["judge_usage_missing"]
    [summary] = fetch(dsn, "SELECT missing_judge_usage_traces FROM trace_summary")
    assert summary["missing_judge_usage_traces"] == 1


def test_concurrent_migrators_are_serialized_and_version_is_recorded_once(make_database):
    dsn = make_database()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(apply_schema, [dsn, dsn]))
    assert fetch(dsn, "SELECT count(*) AS n FROM schema_migrations")[0]["n"] == 1
    assert {"event_id", "agent_id", "eval_lease_token", "eval_cohort", "ingested_at"} <= columns(dsn)
