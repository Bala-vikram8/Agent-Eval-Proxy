import streamlit as st
import psycopg2
import pandas as pd
import os

st.set_page_config(page_title="AgentEval Dashboard", layout="wide")
st.title("AgentEval Dashboard")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://agenteval:agenteval@postgres:5432/agenteval")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

try:
    conn = get_conn()
    df = pd.read_sql("""
        SELECT id, model, latency_ms, cost_usd, faithfulness_score, created_at
        FROM traces
        ORDER BY created_at DESC
        LIMIT 20
    """, conn)
    conn.close()
except Exception as e:
    st.error(f"Database error: {e}")
    df = pd.DataFrame()

if not df.empty:
    col1, col2, col3 = st.columns(3)
    col1.metric("Total Traces", len(df))
    col2.metric("Avg Latency (ms)", round(df["latency_ms"].mean(), 1))
    col3.metric("Total Cost ($)", round(df["cost_usd"].sum(), 4))

    scored = df[df["faithfulness_score"].notna()]
    if not scored.empty:
        st.subheader("Evaluation Scores")
        st.metric("Avg Answer Relevance", round(scored["faithfulness_score"].mean(), 2))

    st.subheader("Recent Traces")
    st.dataframe(df)

    low = scored[scored["faithfulness_score"] < 0.7]
    if not low.empty:
        st.subheader("Low Answer-Relevance Examples")
        for _, row in low.iterrows():
            with st.expander(f"Trace {row['id']} - Score {row['faithfulness_score']}"):
                st.write(row.to_dict())
else:
    st.info("No traces yet. Run the demo to generate some.")