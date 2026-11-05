"""AgentEval dashboard. All aggregation happens in the SQL views defined in sql/init.sql."""
import hmac
import json
import os
from contextlib import closing

import httpx
import pandas as pd
import psycopg2
import streamlit as st

import reviews

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://agenteval:agenteval@postgres:5432/agenteval")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD") or None
PROXY_READY_URL = os.getenv("PROXY_READY_URL") or None
WORKER_STALE_SECONDS = int(os.getenv("ALERT_WORKER_STALE_SECONDS", "60"))
BACKLOG_ALERT_SECONDS = int(os.getenv("ALERT_EVAL_BACKLOG_SECONDS", "300"))


def query(sql: str, params=None) -> pd.DataFrame:
    with closing(psycopg2.connect(DATABASE_URL)) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)


def ms(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value):,.0f} ms"


def usd(value) -> str:
    value = float(value or 0)
    return f"${value:,.6f}" if value < 0.01 else f"${value:,.4f}"


def num(value) -> int:
    return 0 if value is None or pd.isna(value) else int(value)


st.set_page_config(page_title="AgentEval", layout="wide")
st.title("AgentEval")

if DASHBOARD_PASSWORD:
    if not st.session_state.get("authenticated"):
        entered = st.text_input("Dashboard password", type="password")
        if entered and hmac.compare_digest(entered, DASHBOARD_PASSWORD):
            st.session_state["authenticated"] = True
            st.rerun()
        if entered:
            st.error("Wrong password.")
        st.stop()
else:
    st.caption("No DASHBOARD_PASSWORD is set. Keep the dashboard bound to localhost.")

st.caption("Every chat completion that went through the proxy, with latency, cost, evaluations and reviews. "
           "Evaluations and reviews happen after the response has already reached the caller.")

try:
    summary = query("SELECT * FROM trace_summary").iloc[0]
except psycopg2.Error as exc:
    st.error(f"Cannot read from Postgres: {type(exc).__name__}")
    st.stop()

if int(summary["traces"]) == 0:
    st.info("No traces yet. Send a request through the proxy or run the demo agent.")
    st.stop()

# ---- operational alerts ---------------------------------------------------------------
alerts = []
backlog = query("SELECT * FROM eval_backlog")
heartbeats = query("SELECT worker_id, last_seen_at, processed, failures, last_error, judge_model, "
                   "extract(epoch FROM now() - last_seen_at)::int AS seconds_since FROM worker_heartbeats")
if heartbeats.empty:
    alerts.append("No eval worker has reported a heartbeat.")
elif int(heartbeats["seconds_since"].min()) > WORKER_STALE_SECONDS:
    alerts.append(f"No eval worker heartbeat for {int(heartbeats['seconds_since'].min())} s.")
for item in backlog.itertuples():
    if num(item.oldest_pending_seconds) > BACKLOG_ALERT_SECONDS:
        alerts.append(f"{item.eval_type} evaluations: oldest pending item waited {num(item.oldest_pending_seconds)} s.")
    if num(item.expired_leases):
        alerts.append(f"{item.eval_type} evaluations: {num(item.expired_leases)} expired leases (a worker stalled or died).")
proxy_ready = None
if PROXY_READY_URL:
    try:
        proxy_ready = httpx.get(PROXY_READY_URL, timeout=2).json()
    except (httpx.HTTPError, ValueError):
        alerts.append("Proxy readiness endpoint is unreachable.")
if proxy_ready:
    store = proxy_ready["trace_store"]
    if store["postgres_ok"] is False:
        alerts.append(f"Proxy cannot reach Postgres; {store['spool_depth']} traces are waiting in its spool.")
    if store["dropped"]:
        alerts.append(f"Proxy dropped {store['dropped']} traces because its spool was full or unwritable.")
    if store["dead_letter_rows"]:
        alerts.append(f"{store['dead_letter_rows']} traces were rejected by Postgres and are in the dead letter table.")
for alert in alerts:
    st.error(alert)

# ---- traffic --------------------------------------------------------------------------
row = st.columns(5)
row[0].metric("Traces", f"{int(summary['traces']):,}")
row[1].metric("Error rate", f"{float(summary['error_rate_pct']):.1f}%",
              help=f"{int(summary['errors'])} failed: non 200 responses and upstream failures mid stream")
row[2].metric("p50 latency", ms(summary["p50_latency_ms"]), help="Completed calls, full response time")
row[3].metric("p95 latency", ms(summary["p95_latency_ms"]), help="Completed calls, full response time")
row[4].metric("p50 first token, streams", ms(summary["p50_ttft_ms"]),
              help=f"Time to first token over {int(summary['streamed'])} streamed calls; p95 {ms(summary['p95_ttft_ms'])}")

row = st.columns(5)
row[0].metric("Model cost", usd(summary["model_cost_usd"]), help="Token counts priced from the model_prices table")
row[1].metric("Judge cost", usd(summary["judge_cost_usd"]), help="What the relevance judge itself cost")
avg = summary["avg_relevance"]
row[2].metric("Avg relevance", "n/a" if avg is None or pd.isna(avg) else f"{float(avg):.2f}",
              help=f"Mean judge score over {int(summary['eval_scored'])} scored traces. Relevance is not correctness.")
row[3].metric("Scored / skipped", f"{int(summary['eval_scored'])} / {int(summary['eval_skipped'])}",
              help="Skipped: upstream errors and tool call turns with no text to judge")
row[4].metric("Eval pending / failed", f"{int(summary['eval_pending'])} / {int(summary['eval_failed'])}")

if int(summary["unpriced_traces"]):
    st.warning(
        f"{int(summary['unpriced_traces'])} successful traces use models with no row in model_prices, "
        "so their cost is not counted. Add a price, for example: "
        "INSERT INTO model_prices (model, input_usd_per_mtok, output_usd_per_mtok) VALUES ('my-model', 1.00, 4.00);"
    )
if int(summary["usage_missing"]):
    st.warning(f"{int(summary['usage_missing'])} successful traces have no token usage (for example a stream "
               "the caller abandoned), so their cost is unknown, not zero.")

# ---- operations -----------------------------------------------------------------------
st.subheader("Operations")
ops = st.columns(4)
ops[0].metric("p95 ingestion delay", ms(summary["p95_ingest_delay_ms"]),
              help="Time from the request to the row landing in Postgres; grows during database outages")
ops[1].metric("Client disconnects", f"{int(summary['client_disconnects'])}",
              help="Streams the caller abandoned. Not counted as errors, excluded from latency percentiles")
ops[2].metric("Traces with redactions", f"{int(summary['redacted_traces'])}")
if proxy_ready:
    ops[3].metric("Proxy spool", f"{proxy_ready['trace_store']['spool_depth']} queued",
                  help=f"Status: {proxy_ready['status']}")
else:
    ops[3].metric("Proxy spool", "n/a", help="Set PROXY_READY_URL to show the proxy's ingestion state")
st.dataframe(backlog, hide_index=True, width="stretch")
if not heartbeats.empty:
    st.dataframe(heartbeats, hide_index=True, width="stretch")

st.subheader("By model")
st.caption("model is what the caller asked for; response_model is what the provider reports actually served it.")
st.dataframe(query("SELECT * FROM model_summary"), hide_index=True, width="stretch")

# ---- grounded evaluations ---------------------------------------------------------------
grounded_summary = query("SELECT * FROM evaluation_summary")
if not grounded_summary.empty:
    st.subheader("Evidence grounded evaluations")
    st.caption("Checks the reply against the tool results the agent received. Separate from relevance: "
               "a relevant reply can still be unsupported.")
    st.dataframe(grounded_summary, hide_index=True, width="stretch")

# ---- human review -----------------------------------------------------------------------
review_summary = query("SELECT * FROM review_summary").iloc[0]
if num(review_summary["reviews"]):
    st.subheader("Human review")
    st.caption("Retrospective: approving or rejecting here records a decision about a response the agent "
               "already returned. It does not block, change or recall that response.")
    cols = st.columns(5)
    for i, name in enumerate(("pending", "approved", "rejected", "resolved")):
        cols[i].metric(f"Reviews {name}", f"{num(review_summary[name])}")
    cols[4].metric("Oldest pending", f"{num(review_summary['oldest_pending_seconds'])} s")

    reviewer = st.text_input("Your name, recorded on every decision", key="reviewer")
    open_items = query("""
        SELECT r.id, r.status, r.version, r.case_ref, r.reason, r.findings, r.created_at, r.decided_by,
               r.decision_note, t.trace_id
        FROM review_items r JOIN traces t ON t.id = r.trace_id
        WHERE r.status <> 'resolved' ORDER BY r.status = 'pending' DESC, r.id LIMIT 20""")
    for item in open_items.itertuples():
        findings = item.findings or {}
        with st.expander(f"Review #{item.id} [{item.status}] {item.case_ref or ''}: {findings.get('verdict')}"):
            st.markdown(f"**Trace** `{item.trace_id}` · **Opened** {item.created_at:%Y-%m-%d %H:%M %Z}")
            st.markdown("**Why it was flagged**")
            for reason in findings.get("reasons", []):
                st.markdown(f"- {reason}")
            st.markdown("**Request**")
            st.text(findings.get("request", ""))
            st.markdown("**Agent recommendation (already returned to the caller)**")
            st.text(findings.get("reply", ""))
            st.markdown("**Evidence the agent had**")
            for doc in findings.get("evidence", []):
                st.text(f"{doc['id']} {doc['source']}: {doc['text'][:1500]}")
            judge_layer = (findings.get("layers") or {}).get("llm_judge")
            if isinstance(judge_layer, dict) and judge_layer.get("claims"):
                st.markdown("**Claim by claim (LLM judge)**")
                st.dataframe(pd.DataFrame(judge_layer["claims"]), hide_index=True, width="stretch")
            if item.decided_by:
                st.markdown(f"**Decision** by {item.decided_by}: {item.decision_note or ''}")

            options = sorted(to for frm, to in reviews.TRANSITIONS if frm == item.status)
            with st.form(key=f"review-{item.id}-{item.version}"):
                action = st.radio("Decision", options, horizontal=True, key=f"action-{item.id}-{item.version}")
                note = st.text_area("Note (required to reject)", key=f"note-{item.id}-{item.version}")
                if st.form_submit_button("Record decision"):
                    try:
                        with closing(psycopg2.connect(DATABASE_URL)) as conn:
                            reviews.transition(conn, item.id, to_status=action, actor=reviewer,
                                               expected_status=item.status, expected_version=item.version,
                                               note=note)
                        st.success(f"Review #{item.id} is now {action}.")
                        st.rerun()
                    except reviews.ReviewError as exc:
                        st.error(str(exc))

    history = query("""SELECT e.created_at, e.review_id, r.case_ref, e.from_status, e.to_status, e.actor, e.note
                       FROM review_events e JOIN review_items r ON r.id = e.review_id
                       ORDER BY e.id DESC LIMIT 50""")
    st.markdown("**Audit trail** (append only)")
    st.dataframe(history, hide_index=True, width="stretch")

# ---- traces -----------------------------------------------------------------------------
st.subheader("Recent traces")
st.dataframe(
    query("""
        SELECT id, trace_id, created_at, case_ref, model, response_model, status_code, error_type, streamed,
               latency_ms, ttft_ms, total_tokens, round(cost_usd, 6) AS cost_usd, eval_status, relevance_score
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
        st.text(user if isinstance(user, str) else json.dumps(user))
        st.markdown("**Assistant reply**")
        st.text(reply or "")
