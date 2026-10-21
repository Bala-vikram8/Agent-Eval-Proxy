import os
import streamlit as st
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db():
    return psycopg2.connect(DATABASE_URL)

st.set_page_config(page_title="AgentEval Dashboard", layout="wide")
st.title("AgentEval Dashboard")

conn = get_db()
cur = conn.cursor(cursor_factory=RealDictCursor)

# Metrics
cur.execute("SELECT COUNT(*) as total, AVG(latency_ms) as avg_latency, SUM(cost_usd) as total_cost FROM traces")
metrics = cur.fetchone()

col1, col2, col3 = st.columns(3)
col1.metric("Total Traces", metrics["total"] or 0)
col2.metric("Avg Latency (ms)", round(metrics["avg_latency"] or 0, 1))
col3.metric("Total Cost ($)", round(float(metrics["total_cost"] or 0), 4))

# Evaluations
cur.execute("""
    SELECT AVG(faithfulness_score) as avg_faith, 
           AVG(CASE WHEN tool_correctness THEN 1 ELSE 0 END) as tool_rate
    FROM evaluations
""")
evals = cur.fetchone()
st.subheader("Evaluation Scores")
col4, col5 = st.columns(2)
col4.metric("Avg Faithfulness", round(float(evals["avg_faith"] or 0), 2))
col5.metric("Tool Correctness Rate", round(float(evals["tool_rate"] or 0) * 100, 1))

# Recent traces
st.subheader("Recent Traces")
cur.execute("""
    SELECT id, model, latency_ms, cost_usd, created_at
    FROM traces
    ORDER BY created_at DESC
    LIMIT 20
""")
traces = cur.fetchall()
if traces:
    df = pd.DataFrame(traces)
    st.dataframe(df)

# Failure clusters (simple: low score evals)
st.subheader("Low Faithfulness Examples")
cur.execute("""
    SELECT t.id, t.messages, e.faithfulness_score, e.judge_reason
    FROM traces t
    JOIN evaluations e ON t.id = e.trace_id
    WHERE e.faithfulness_score < 0.6
    ORDER BY e.created_at DESC
    LIMIT 5
""")
failures = cur.fetchall()
for f in failures:
    with st.expander(f"Trace {f['id']} - Score {f['faithfulness_score']}"):
        st.json(f["messages"])
        st.write(f["judge_reason"])

cur.close()
conn.close()
