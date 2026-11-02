"""Reproducible SIMULATED customer integration and troubleshooting exercise.

Creates only a temporary database on TEST_DATABASE_URL, loopback processes and
synthetic traffic. Never uses real dealerships, customers or provider APIs.
"""
import argparse
import contextlib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
import psycopg2
from psycopg2 import sql
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
from monitoring import compare_quality  # noqa: E402
from monitor import run_once as monitor_once  # noqa: E402
try:
    from .automotive_upstream import EXPECTED, MODEL
except ImportError:
    from automotive_upstream import EXPECTED, MODEL

TOOL_SCHEMA = {"type": "object", "properties": {"document_id": {"type": "string", "enum": ["SIM-WO-7"]}},
               "required": ["document_id"], "additionalProperties": False}
TOOLS = [{"type": "function", "function": {"name": "lookup_workflow_document",
          "description": "Read a simulated internal work-order routing document", "parameters": TOOL_SCHEMA}}]


def lookup_document(name, arguments):
    if name != "lookup_workflow_document":
        raise ValueError("Tool is not allowed")
    value = json.loads(arguments)
    if value != {"document_id": "SIM-WO-7"}:
        raise ValueError("Document arguments are invalid")
    return json.dumps({"document_id": "SIM-WO-7", "simulated": True, "instruction": EXPECTED})


def free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def process(command, cwd, env, log_path, health=None):
    with open(log_path, "w") as log:
        child = subprocess.Popen(command, cwd=cwd, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT)
        try:
            if health:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError(f"Scenario process failed; see {log_path}")
                    try:
                        if httpx.get(health, timeout=1).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise TimeoutError(f"Scenario readiness failed; see {log_path}")
            yield child
        finally:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def query(dsn, statement, params=None):
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute(statement, params)
        return cur.fetchall()


def run(args):
    admin = os.environ.get("TEST_DATABASE_URL")
    if not admin:
        raise SystemExit("Set TEST_DATABASE_URL to a LOCAL development admin database.")
    # This exercise never accepts a remote database: no production deletion risk.
    parsed = psycopg2.extensions.parse_dsn(admin)
    if parsed.get("host") not in ("127.0.0.1", "localhost"):
        raise SystemExit("This simulated exercise requires a loopback database host.")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("NO_PROXY", "no_proxy"):
        os.environ[name] = os.environ.get(name, "") + ",127.0.0.1,localhost"
    database = "agenteval_sim_" + uuid.uuid4().hex[:12]
    dsn = psycopg2.extensions.make_dsn(admin, dbname=database)
    with contextlib.closing(psycopg2.connect(admin)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        with contextlib.closing(psycopg2.connect(dsn)) as conn, conn, conn.cursor() as cur:
            cur.execute((ROOT / "sql/init.sql").read_text())
        with tempfile.TemporaryDirectory(prefix="agenteval-automotive-") as work, contextlib.ExitStack() as stack:
            work = Path(work)
            catalog = work / "references.json"
            catalog.write_text(json.dumps({"automotive-routing-v1": {
                "reference_answer": EXPECTED, "evidence": [EXPECTED],
                "cohort": "synthetic-parts-routing-v1", "tool_schemas": {"lookup_workflow_document": TOOL_SCHEMA}}}))
            catalog.chmod(0o600)
            upstream_port, judge_port, proxy_port = free_port(), free_port(), free_port()
            upstream, judge, proxy = [f"http://127.0.0.1:{p}" for p in (upstream_port, judge_port, proxy_port)]
            common = {"ALLOW_INSECURE_LOCAL": "true", "TRACE_CONTENT_MODE": "redacted", "DATABASE_URL": dsn,
                      "TRACE_SPOOL_PATH": str(work / "spool.sqlite3"), "TRACE_QUEUE_BACKEND": "sqlite",
                      "EVAL_REFERENCE_CATALOG": str(catalog), "EVAL_POLL_SECONDS": "0.1"}
            def serve(directory, module, port, url):
                stack.enter_context(process([sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1",
                    "--port", str(port), "--log-level", "warning"], ROOT / directory, common,
                    output / f"{directory}.log", url))
            serve("examples", "automotive_upstream:app", upstream_port, upstream + "/health")
            serve("mock_upstream", "server:app", judge_port, judge + "/v1/models")
            common.update(UPSTREAM_BASE=upstream, UPSTREAM_API_KEY="synthetic-only",
                          JUDGE_BASE_URL=judge + "/v1", JUDGE_API_KEY="synthetic-only")
            serve("proxy", "main:app", proxy_port, proxy + "/health")
            stack.enter_context(process([sys.executable, "eval_engine.py"], ROOT / "eval", common,
                                        output / "eval.log"))
            client = OpenAI(base_url=proxy + "/v1", api_key="synthetic-only", max_retries=0,
                            default_headers={"x-agent-id": "simulated-service-workflow"})
            markers, latencies = {}, []
            started = time.monotonic()
            for phase, broken in (("baseline", False), ("incident", True), ("recovered", False)):
                httpx.post(upstream + "/scenario/config", json={"broken": broken}).raise_for_status()
                markers[phase] = []
                for i in range(args.samples):
                    messages = [{"role": "user", "content": f"SIMULATED work order {phase}-{i}: a parts order is on hold. Which queue receives it?"}]
                    metadata = {"evaluation_context": json.dumps({"reference_id": "automotive-routing-v1"})}
                    first = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, metadata=metadata)
                    call = first.choices[0].message.tool_calls[0]
                    messages.extend([{"role": "assistant", "content": None, "tool_calls": [{
                        "id": call.id, "type": "function", "function": {"name": call.function.name,
                        "arguments": call.function.arguments}}]}, {"role": "tool", "tool_call_id": call.id,
                        "content": lookup_document(call.function.name, call.function.arguments)}])
                    before = time.perf_counter()
                    response = client.chat.completions.with_raw_response.create(model=MODEL, messages=messages, metadata=metadata)
                    answer = response.parse().choices[0].message.content
                    latencies.append((time.perf_counter() - before) * 1000)
                    markers[phase].append(response.headers["x-trace-id"])
                    if not broken and answer != EXPECTED:
                        raise AssertionError("Configuration fix did not restore the expected routing answer")
            deadline = time.monotonic() + 90
            all_ids = [marker for phase in markers.values() for marker in phase]
            while time.monotonic() < deadline:
                count = query(dsn, "SELECT count(*) FROM evaluation_runs e JOIN traces t ON t.id=e.trace_id "
                              "WHERE t.event_id = ANY(%s) AND e.evaluator_name='reference_exact' AND e.score IS NOT NULL", (all_ids,))[0][0]
                if count == len(all_ids):
                    break
                time.sleep(.2)
            else:
                raise TimeoutError("Reference evaluations did not complete; inspect scenario logs")
            scores = {phase: [float(r[0]) for r in query(dsn,
                "SELECT e.score FROM evaluation_runs e JOIN traces t ON t.id=e.trace_id "
                "WHERE t.event_id=ANY(%s) AND e.evaluator_name='reference_exact' ORDER BY t.id", (ids,))]
                for phase, ids in markers.items()}
            cohort = ("simulated-service-workflow", MODEL, "synthetic-parts-routing-v1", "reference_exact", "1.0.0")
            incident = compare_quality(scores["baseline"], scores["incident"], baseline_cohort=cohort, current_cohort=cohort)
            recovery = compare_quality(scores["baseline"], scores["recovered"], baseline_cohort=cohort, current_cohort=cohort)
            assert incident.status == "quality_deterioration"
            assert recovery.status == "no_supported_deterioration"
            assert scores["baseline"] == scores["recovered"] == [1.0] * args.samples
            assert scores["incident"] == [0.0] * args.samples
            # A controlled clock avoids waiting two days for the monitoring
            # windows. This mutates only this generated synthetic database.
            end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            with contextlib.closing(psycopg2.connect(dsn)) as connection, connection, connection.cursor() as cur:
                for phase, hours in (("baseline", -36), ("incident", -12), ("recovered", 12)):
                    cur.execute("UPDATE traces SET created_at=%s WHERE event_id=ANY(%s)",
                                (end + timedelta(hours=hours), markers[phase]))
            with contextlib.closing(psycopg2.connect(dsn)) as connection:
                connection.autocommit = True
                monitored = monitor_once(connection, now=end)
                assert monitored["alerts_inserted"] >= 1
                # Rechecking the same window must not create duplicate alerts.
                assert monitor_once(connection, now=end)["alerts_inserted"] == 0
                after_fix = monitor_once(connection, now=end + timedelta(hours=24))
                assert after_fix["alerts_inserted"] == 0
            dashboard_code = """
import sys
from streamlit.testing.v1 import AppTest
app = AppTest.from_file(sys.argv[1], default_timeout=30).run()
assert not app.exception, app.exception
assert any(m.label == 'Traces' and int(m.value.replace(',', '')) > 0 for m in app.metric)
assert any(b.label == 'Acknowledge alert' for b in app.button)
next(b for b in app.button if b.label == 'Acknowledge alert').click().run()
assert not app.exception, app.exception
print('Synthetic-mode dashboard investigation and acknowledgement passed')
"""
            rendered = subprocess.run([sys.executable, "-c", dashboard_code, str(ROOT / "dashboard/app.py")],
                cwd=ROOT, env={**os.environ, **common, "DASHBOARD_ALLOW_ALERT_ACK": "true",
                               "DASHBOARD_OPERATOR_DATABASE_URL": dsn},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
            (output / "dashboard.log").write_text(rendered.stdout)
            if rendered.returncode:
                raise RuntimeError("Dashboard investigation failed; inspect dashboard.log")
            assert query(dsn, "SELECT count(*) FROM alerts WHERE status='acknowledged'")[0][0] >= 1
            total = query(dsn, "SELECT count(*) FROM traces")[0][0]
            assert total == args.samples * 6, "Every tool and answer request must be captured"
            elapsed = time.monotonic() - started
            report = {"simulated": True, "real_customer": False, "samples_per_phase": args.samples,
                "captured_traces": total, "expected_traces": args.samples * 6, "elapsed_seconds": elapsed,
                "model_calls_per_second_including_evaluation_drain": total / elapsed,
                "answer_http_latency_ms": {"median": sorted(latencies)[len(latencies)//2],
                    "p95": sorted(latencies)[min(len(latencies)-1, int(len(latencies)*.95))]},
                "incident": asdict(incident), "recovery": asdict(recovery),
                "persisted_alerts_inserted": monitored["alerts_inserted"],
                "recovery_new_alerts": after_fix["alerts_inserted"],
                "dashboard_investigation_and_acknowledgement": True,
                "monitoring_time_simulated": True,
                "provider_spend_usd": 0, "cost_limitations": "Mock tokens and judge scores are synthetic; infrastructure cost excluded.",
                "limitations": "Independent synthetic work orders, exact known reference, deterministic model. No claim of general correctness or live deployment."}
            (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
    finally:
        with contextlib.closing(psycopg2.connect(admin)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--output", default="artifacts/automotive")
    arguments = parser.parse_args()
    if not 30 <= arguments.samples <= 500:
        parser.error("samples must be between 30 and 500")
    run(arguments)
