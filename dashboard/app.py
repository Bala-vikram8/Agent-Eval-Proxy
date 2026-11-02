"""Authenticated single-deployment monitoring. Filters are not tenant isolation."""
import logging
import os
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd
import psycopg2
import streamlit as st

from auth import may_acknowledge_alerts, require_authentication

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://agenteval:agenteval@postgres:5432/agenteval')
log = logging.getLogger('agenteval.dashboard')


def query(statement: str, params=()) -> pd.DataFrame:
    with closing(psycopg2.connect(DATABASE_URL, connect_timeout=5,
                                 options='-c statement_timeout=5000')) as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


def operator_dsn() -> str | None:
    path = os.getenv('DASHBOARD_OPERATOR_DATABASE_URL_FILE')
    if path:
        try:
            return Path(path).read_text().strip() or None
        except (OSError, UnicodeError):
            return None
    return os.getenv('DASHBOARD_OPERATOR_DATABASE_URL') or None


def acknowledge(alert_id: int) -> None:
    dsn = operator_dsn()
    if not may_acknowledge_alerts() or not dsn:
        raise PermissionError('Alert acknowledgement is disabled')
    with closing(psycopg2.connect(dsn, connect_timeout=5,
                                 options='-c statement_timeout=5000 -c lock_timeout=2000')) as conn, conn, conn.cursor() as cur:
        cur.execute("""UPDATE alerts SET status='acknowledged', acknowledged_at=now(),
                       acknowledged_by=%s WHERE id=%s AND status='open'""", ('dashboard-operator', alert_id))


def ms(value) -> str:
    return 'n/a' if value is None or pd.isna(value) else f'{float(value):,.0f} ms'


def usd(value) -> str:
    value = float(value or 0)
    return f'${value:,.6f}' if value < 0.01 else f'${value:,.4f}'


def scope_filters() -> tuple[str, list]:
    st.sidebar.caption('UTC dates; filters apply to metrics, history, and trace results.')
    today_utc = datetime.now(timezone.utc).date()
    begin = st.sidebar.date_input('From date', today_utc - timedelta(days=30))
    end = st.sidebar.date_input('Through date', today_utc)
    if end < begin:
        st.error('Choose an end date on or after the start date.')
        st.stop()
    clauses = ['created_at >= %s', 'created_at < %s']
    params = [datetime.combine(begin, time.min, timezone.utc),
              datetime.combine(end + timedelta(days=1), time.min, timezone.utc)]
    options = query('SELECT DISTINCT agent_id,model FROM traces ORDER BY agent_id,model LIMIT 500')
    agents = sorted(str(x) for x in options.get('agent_id', []) if x is not None)
    models = sorted(str(x) for x in options.get('model', []) if x is not None)
    agent = st.sidebar.selectbox('Agent', ['All', *dict.fromkeys(agents)])
    model = st.sidebar.selectbox('Model', ['All', *dict.fromkeys(models)])
    status = st.sidebar.selectbox('Evaluation status', ['All', 'pending', 'processing', 'scored', 'skipped', 'failed'])
    response = st.sidebar.selectbox('API status', ['All', 'Success', 'Error'])
    for column, value in [('agent_id', agent), ('model', model), ('eval_status', status)]:
        if value != 'All':
            clauses.append(column + ' = %s')
            params.append(value)
    if response != 'All':
        clauses.append('status_code = 200' if response == 'Success' else 'status_code IS DISTINCT FROM 200')
    return ' AND '.join(clauses), params


def main() -> None:
    st.set_page_config(page_title='AgentEval', layout='wide')
    require_authentication()  # Must precede all database access.
    st.title('AgentEval')
    st.caption('Single-deployment monitoring. Relevance scores are judge opinions, not verified factual correctness.')
    if st.button('Refresh data'):
        st.rerun()
    where, params = scope_filters()
    summary = query("""SELECT count(*) AS traces,
        count(*) FILTER (WHERE status_code IS DISTINCT FROM 200) AS errors,
        100.0*count(*) FILTER (WHERE status_code IS DISTINCT FROM 200)/nullif(count(*),0) AS error_rate_pct,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE status_code=200) AS p50_latency_ms,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE status_code=200) AS p95_latency_ms,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY ttft_ms) FILTER (WHERE streamed AND status_code=200) AS p50_ttft_ms,
        coalesce(sum(cost_usd),0) AS model_cost_usd,coalesce(sum(judge_cost_usd),0) AS judge_cost_usd,
        count(*) FILTER (WHERE unpriced) AS unpriced_traces,
        count(*) FILTER (WHERE usage_missing) AS missing_usage_traces,
        count(*) FILTER (WHERE judge_model IS NOT NULL AND
            (judge_prompt_tokens IS NULL OR judge_completion_tokens IS NULL)) AS missing_judge_usage_traces,
        count(*) FILTER (WHERE judge_unpriced) AS unpriced_judge_traces,
        avg(relevance_score) AS avg_relevance,
        count(*) FILTER (WHERE eval_status='scored') AS eval_scored,
        count(*) FILTER (WHERE eval_status='skipped') AS eval_skipped,
        count(*) FILTER (WHERE eval_status='pending') AS eval_pending,
        count(*) FILTER (WHERE eval_status='processing') AS eval_processing,
        count(*) FILTER (WHERE eval_status='failed') AS eval_failed
        FROM traces_enriched WHERE """ + where, params).iloc[0]
    st.success('Database is reachable. Service and queue health require the worker signals below.')
    if int(summary['traces']) == 0:
        st.info('No traces yet in this scope. Send a request through the proxy, run the demo, or change filters.')
    else:
        render_metrics(summary)
        render_history(where, params)
        render_traces(where, params)
    render_operations(where, params)


def render_metrics(summary) -> None:
    row = st.columns(5)
    row[0].metric('Traces', f"{int(summary['traces']):,}")
    row[1].metric('Error rate', f"{float(summary['error_rate_pct']):.1f}%")
    row[2].metric('p50 latency', ms(summary['p50_latency_ms']))
    row[3].metric('p95 latency', ms(summary['p95_latency_ms']))
    row[4].metric('p50 first token, streams', ms(summary['p50_ttft_ms']))
    row = st.columns(5)
    row[0].metric('Model cost', usd(summary['model_cost_usd']))
    row[1].metric('Judge cost', usd(summary['judge_cost_usd']))
    avg = summary['avg_relevance']
    row[2].metric('Avg relevance', 'n/a' if avg is None or pd.isna(avg) else f'{float(avg):.2f}')
    row[3].metric('Scored / skipped', f"{int(summary['eval_scored'])} / {int(summary['eval_skipped'])}")
    row[4].metric('Eval pending / failed', f"{int(summary['eval_pending'])} / {int(summary['eval_failed'])}")
    st.caption(f"Processing evaluations: {int(summary['eval_processing'])}. Missing content, skipped checks, and failed checks are not passing evaluations.")
    if int(summary['unpriced_traces']):
        st.warning(f"{int(summary['unpriced_traces'])} successful traces have no row in model_prices; their cost is not counted.")
    if (int(summary['unpriced_judge_traces']) or int(summary['missing_usage_traces'])
            or int(summary['missing_judge_usage_traces'])):
        st.warning('Some judge prices or token usage are missing. Displayed costs are incomplete.')


def render_history(where, params) -> None:
    st.subheader('Agent and model performance')
    st.dataframe(query("""SELECT agent_id,model,count(*) AS requests,
        count(*) FILTER (WHERE status_code IS DISTINCT FROM 200) AS errors,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
        sum(total_tokens) AS tokens,round(sum(cost_usd),6) AS model_cost_usd,
        round(avg(relevance_score),2) AS avg_relevance FROM traces_enriched WHERE """ + where +
        ' GROUP BY agent_id,model ORDER BY requests DESC LIMIT 100', params), hide_index=True, width='stretch')
    st.subheader('Daily history (UTC)')
    history = query("""SELECT date_trunc('day',created_at) AS day,count(*) AS requests,
        count(*) FILTER (WHERE status_code IS DISTINCT FROM 200) AS errors,
        avg(relevance_score) AS relevance,sum(total_tokens) AS tokens,sum(cost_usd) AS cost_usd
        FROM traces_enriched WHERE """ + where + ' GROUP BY day ORDER BY day LIMIT 366', params)
    st.dataframe(history, hide_index=True, width='stretch')
    if not history.empty:
        st.line_chart(history.set_index('day')[['requests', 'errors']])
        st.line_chart(history.set_index('day')[['relevance']].astype('float64'))


def render_traces(where, params) -> None:
    st.subheader('Trace search and investigation')
    search = st.text_input('Exact trace, event, or request ID', max_chars=128).strip()
    if search:
        where += ' AND (id::text=%s OR event_id=%s OR request_id=%s)'
        params = [*params, search, search, search]
    recent = query("""SELECT id,event_id,created_at,agent_id,model,status_code,streamed,
        stream_completed,failure_category,latency_ms,ttft_ms,total_tokens,
        round(cost_usd,6) AS cost_usd,eval_status,relevance_score,content_mode
        FROM traces_enriched WHERE """ + where + ' ORDER BY id DESC LIMIT 100', params)
    st.dataframe(recent, hide_index=True, width='stretch')
    st.caption('At most 100 results. Narrow dates or search by exact ID. Trace metadata may also be sensitive.')
    if not recent.empty:
        chosen = st.selectbox('Inspect trace', [int(x) for x in recent['id']])
        events = query("""SELECT evaluator_name,evaluator_version,config_version,attempt,status,score,
            reason,created_at FROM evaluation_runs WHERE trace_id=%s ORDER BY created_at DESC LIMIT 100""", (chosen,))
        st.dataframe(events, hide_index=True, width='stretch')
        if os.getenv('DASHBOARD_ALLOW_TRACE_CONTENT', '').lower() == 'true':
            detail = query('SELECT messages,response FROM traces WHERE id=%s', (chosen,)).iloc[0]
            st.warning('Content access is enabled for this operator deployment. Historical records may predate redaction.')
            st.json({'messages': detail['messages'], 'response': detail['response']})
        else:
            st.caption('Content access is disabled by the deployment operator; metadata and evaluation history remain available.')
    st.subheader('Low relevance')
    low = query('SELECT id,relevance_score FROM traces_enriched WHERE ' + where +
                " AND eval_status='scored' AND relevance_score<0.7 ORDER BY relevance_score,id DESC LIMIT 10", params)
    if low.empty:
        st.caption('No scored trace is below 0.7 in this scope.')
    for row in low.itertuples():
        with st.expander(f'Trace {row.id}, score {float(row.relevance_score):.2f}'):
            st.caption('Inspect this trace above. Low relevance does not establish incorrectness or harm.')


def render_operations(where, params) -> None:
    st.subheader('Evaluation queue and workers')
    st.dataframe(query("""SELECT eval_status,count(*) AS jobs,
        extract(epoch FROM now()-min(created_at)) AS oldest_trace_age_seconds
        FROM traces WHERE """ + where + ' GROUP BY eval_status ORDER BY eval_status', params),
        hide_index=True, width='stretch')
    workers = query("""SELECT worker_id,worker_type,last_seen,
        extract(epoch FROM now()-last_seen) AS heartbeat_age_seconds,details
        FROM worker_heartbeats ORDER BY last_seen DESC LIMIT 100""")
    if workers.empty:
        st.warning('No worker heartbeat is available. Queue completion alone does not prove worker health.')
    else:
        st.dataframe(workers, hide_index=True, width='stretch')
        if (workers['heartbeat_age_seconds'].astype(float) > 120).any():
            st.warning('At least one worker heartbeat is stale (over 120 seconds).')
    st.caption('Failed evaluations form the evaluation dead-letter set. Trace-ingestion queue and loss counters are exported by the proxy, not inferred from successful database rows.')
    st.subheader('Quality alerts')
    st.caption('Candidate score deterioration uses matched agent/model/request cohort/evaluator versions, minimum sample sizes, and a confidence bound. It does not prove model drift. Within-cohort traffic changes can still confound results.')
    alerts = query("""SELECT id,created_at,agent_id,metric,rule_version,severity,status,message,
        acknowledged_at,acknowledged_by FROM alerts ORDER BY created_at DESC LIMIT 100""")
    st.dataframe(alerts, hide_index=True, width='stretch')
    if may_acknowledge_alerts() and operator_dsn():
        open_ids = [int(row.id) for row in alerts.itertuples() if row.status == 'open']
        if open_ids:
            selected = st.selectbox('Open alert to acknowledge', open_ids)
            if st.button('Acknowledge alert'):
                acknowledge(selected)
                st.rerun()
    else:
        st.caption('Alert acknowledgement requires an explicitly configured operator capability and separate database role.')


try:
    main()
except psycopg2.Error as exc:
    log.warning('dashboard_database_failure error_type=%s', type(exc).__name__)
    st.error('Monitoring data is temporarily unavailable. Ask the deployment operator to check database health.')
