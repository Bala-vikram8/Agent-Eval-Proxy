"""AgentEval dashboard. All aggregation happens in the SQL views defined in sql/init.sql."""
import os
from contextlib import closing

import pandas as pd
import psycopg2
import streamlit as st

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://agenteval:agenteval@postgres:5432/agenteval")


def query(sql: str) -> pd.DataFrame:
    with closing(psycopg2.connect(DATABASE_URL)) as conn, conn.cursor() as cur:
        cur.execute(sql)
        columns = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)


def ms(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value):,.0f} ms"


def usd(value) -> str:
    value = float(value or 0)
    return f"${value:,.6f}" if value < 0.01 else f"${value:,.4f}"


st.set_page_config(page_title="AgentEval", layout="wide")
st.title("AgentEval")
st.caption("Every chat completion that went through the proxy, with latency, cost and judge scores.")

try:
    summary = query("SELECT * FROM trace_summary").iloc[0]
except psycopg2.Error as exc:
    st.error(f"Cannot read from Postgres: {exc}")
    st.stop()

if int(summary["traces"]) == 0:
    st.info("No traces yet. Send a request through the proxy or run the demo agent.")
    st.stop()

row = st.columns(5)
row[0].metric("Traces", f"{int(summary['traces']):,}")
row[1].metric("Error rate", f"{float(summary['error_rate_pct']):.1f}%", help=f"{int(summary['errors'])} non 200 responses")
row[2].metric("p50 latency", ms(summary["p50_latency_ms"]), help="Successful calls, full response time")
row[3].metric("p95 latency", ms(summary["p95_latency_ms"]), help="Successful calls, full response time")
row[4].metric("p50 first token, streams", ms(summary["p50_ttft_ms"]), help=f"Time to first token over {int(summary['streamed'])} streamed calls")

row = st.columns(5)
row[0].metric("Model cost", usd(summary["model_cost_usd"]), help="Token counts priced from the model_prices table")
row[1].metric("Judge cost", usd(summary["judge_cost_usd"]), help="What the relevance judge itself cost")
avg = summary["avg_relevance"]
row[2].metric("Avg relevance", "n/a" if avg is None or pd.isna(avg) else f"{float(avg):.2f}",
              help=f"Mean judge score over {int(summary['eval_scored'])} scored traces")
row[3].metric("Scored / skipped", f"{int(summary['eval_scored'])} / {int(summary['eval_skipped'])}",
              help="Skipped: upstream errors and tool call turns with no text to judge")
row[4].metric("Eval pending / failed", f"{int(summary['eval_pending'])} / {int(summary['eval_failed'])}")

if int(summary["unpriced_traces"]):
    st.warning(
        f"{int(summary['unpriced_traces'])} successful traces use models with no row in model_prices, "
        "so their cost is not counted. Add a price, for example: "
        "INSERT INTO model_prices (model, input_usd_per_mtok, output_usd_per_mtok) VALUES ('my-model', 1.00, 4.00);"
    )

st.subheader("By model")
st.dataframe(query("SELECT * FROM model_summary"), hide_index=True, width="stretch")

st.subheader("Recent traces")
st.dataframe(
    query("""
        SELECT id, created_at, model, status_code, streamed, latency_ms, ttft_ms,
               total_tokens, round(cost_usd, 6) AS cost_usd, eval_status, relevance_score
        FROM traces_enriched ORDER BY id DESC LIMIT 50
    """),
    hide_index=True,
    width="stretch",
)

st.subheader("Low relevance")
low = query("""
    SELECT id, relevance_score, relevance_reason, messages, response
    FROM traces WHERE eval_status = 'scored' AND relevance_score < 0.7
    ORDER BY relevance_score, id DESC LIMIT 10
""")
if low.empty:
    st.caption("No scored trace is below 0.7.")
for item in low.itertuples():
    with st.expander(f"Trace {item.id}, score {float(item.relevance_score):.2f}: {item.relevance_reason}"):
        user = next((m.get("content") for m in reversed(item.messages or []) if m.get("role") == "user"), "")
        reply = ((item.response or {}).get("choices") or [{}])[0].get("message", {}).get("content")
        st.markdown("**User request**")
        st.text(user if isinstance(user, str) else str(user))
        st.markdown("**Assistant reply**")
        st.text(reply or "")
