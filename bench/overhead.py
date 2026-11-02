"""Measures what the proxy adds on top of the upstream call.

Starts the mock upstream and the proxy as local processes against a throwaway Postgres
database, then sends the same chat completion directly to the mock and through the proxy.

  TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres python bench/overhead.py

Numbers depend on the machine, so compare runs on the same box.
"""
import asyncio
import contextlib
from collections import Counter
from dataclasses import dataclass, field
import json
import os
import platform
import resource
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import psycopg2
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[1]
BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "User cannot login before payroll."}]}
SEQUENTIAL_REQUESTS = int(os.getenv("BENCH_SEQUENTIAL", "1000"))
CONCURRENT_REQUESTS = int(os.getenv("BENCH_CONCURRENT", "2000"))
CONCURRENCY = int(os.getenv("BENCH_CONCURRENCY", "50"))
DRAIN_SECONDS = float(os.getenv("BENCH_DRAIN_SECONDS", "30"))
OUTPUT = Path(os.getenv("BENCH_OUTPUT", "artifacts/benchmark.json"))
UPSTREAM_LATENCY_MS = 100  # stands in for model latency in the concurrent run


def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def server(app_dir: str, app: str, env: dict, *, label: str):
    port = free_port()
    log_dir = OUTPUT.parent / (OUTPUT.stem + '-logs')
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / (label + '.log'), 'w')
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning", "--no-access-log"],
        cwd=ROOT / app_dir, env={**os.environ, **env}, stdout=log_file, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            if proc.poll() is not None:
                raise RuntimeError(f"{app_dir} exited before readiness")
            try:
                response = httpx.get(f"{url}/ready" if app_dir == "proxy" else f"{url}/v1/models", timeout=1)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise TimeoutError(f"{app_dir} readiness deadline expired")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_file.close()


@contextlib.contextmanager
def throwaway_database(admin_dsn: str):
    name = f"agenteval_bench_{uuid.uuid4().hex[:8]}"
    with contextlib.closing(psycopg2.connect(admin_dsn)) as conn:
        conn.autocommit = True
        conn.cursor().execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = psycopg2.extensions.make_dsn(admin_dsn, dbname=name)
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        conn.cursor().execute((ROOT / "sql" / "init.sql").read_text())
    try:
        yield dsn
    finally:
        with contextlib.closing(psycopg2.connect(admin_dsn)) as conn:
            conn.autocommit = True
            conn.cursor().execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@dataclass
class LoadResult:
    timings: list[float] = field(default_factory=list)
    failure_timings: list[float] = field(default_factory=list)
    statuses: Counter = field(default_factory=Counter)
    failure_examples: list[dict] = field(default_factory=list)
    durable_event_ids: list[str] = field(default_factory=list)
    warmup_event_ids: list[str] = field(default_factory=list)
    warmup_statuses: Counter = field(default_factory=Counter)
    elapsed: float = 0.0

    def observe(self, response, elapsed_ms: float, *, warmup=False, failure_type=None):
        status = str(response.status_code) if response is not None else 'transport_error'
        (self.warmup_statuses if warmup else self.statuses)[status] += 1
        if response is not None and response.headers.get('x-trace-delivery') == 'durable':
            event_id = response.headers.get('x-trace-id')
            if event_id:
                (self.warmup_event_ids if warmup else self.durable_event_ids).append(event_id)
        ok = response is not None and 200 <= response.status_code < 300
        if not warmup:
            (self.timings if ok else self.failure_timings).append(elapsed_ms)
        if not ok and len(self.failure_examples) < 10:
            code = failure_type or 'http_error'
            if response is not None:
                try:
                    data = response.json()
                    candidate = data.get('error', {}).get('code') if isinstance(data, dict) else None
                    if isinstance(candidate, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', candidate):
                        code = candidate
                except (ValueError, AttributeError, RecursionError):
                    pass
            self.failure_examples.append({'status': status, 'code': code, 'warmup': warmup})

    @property
    def failures(self):
        return sum(n for status,n in [*self.statuses.items(), *self.warmup_statuses.items()]
                   if not status.isdigit() or not 200 <= int(status) < 300)

    def summary(self):
        return {'status_counts': dict(self.statuses), 'warmup_status_counts': dict(self.warmup_statuses),
                'failed_requests': self.failures, 'failure_examples': self.failure_examples,
                'success_p50_ms': percentile(self.timings, 50), 'success_p95_ms': percentile(self.timings, 95),
                'failure_p50_ms': percentile(self.failure_timings, 50),
                'failure_p95_ms': percentile(self.failure_timings, 95), 'elapsed_seconds': self.elapsed,
                'successful_requests_per_second': len(self.timings)/self.elapsed if self.elapsed else None}


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def sequential(url: str, n: int) -> LoadResult:
    result = LoadResult()
    with httpx.Client(base_url=url, timeout=30) as client:
        for warmup, count in ((True, 50), (False, n)):
            began = time.perf_counter()
            for _ in range(count):
                started = time.perf_counter()
                try:
                    response = client.post('/v1/chat/completions', json=BODY)
                except httpx.HTTPError as exc:
                    result.observe(None, (time.perf_counter()-started)*1000,
                                   warmup=warmup, failure_type=type(exc).__name__)
                else:
                    result.observe(response, (time.perf_counter()-started)*1000, warmup=warmup)
            if not warmup:
                result.elapsed = time.perf_counter()-began
    return result


async def concurrent(url: str, n: int, concurrency: int) -> LoadResult:
    result = LoadResult()
    queue = iter(range(n))
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=60, limits=limits) as client:
        async def worker():
            for _ in queue:
                started = time.perf_counter()
                try:
                    response = await client.post('/v1/chat/completions', json=BODY)
                except httpx.HTTPError as exc:
                    result.observe(None, (time.perf_counter()-started)*1000, failure_type=type(exc).__name__)
                else:
                    result.observe(response, (time.perf_counter()-started)*1000)
        began = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        result.elapsed = time.perf_counter()-began
    return result


def count_traces(dsn: str) -> int:
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM traces")
        return cur.fetchone()[0]


def cpu_quota_cores():
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        return None if quota == 'max' else int(quota) / int(period)
    except (OSError, ValueError, ZeroDivisionError):
        return None


def main() -> None:
    admin_dsn = os.getenv("TEST_DATABASE_URL")
    if not admin_dsn:
        sys.exit("Set TEST_DATABASE_URL to a Postgres server this script may create a database on.")
    if min(SEQUENTIAL_REQUESTS, CONCURRENT_REQUESTS) < 2 or CONCURRENCY < 1 or DRAIN_SECONDS <= 0:
        sys.exit("Benchmark counts must be at least two, and concurrency/drain deadline positive.")
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = os.environ.get(key, "") + ",127.0.0.1,localhost"
    local = {"ALLOW_INSECURE_LOCAL": "true", "TRACE_CONTENT_MODE": "metadata",
             "TRACE_QUEUE_BACKEND": "sqlite", "TRACE_INGEST_EMBEDDED": "true",
             "PROXY_REQUESTS_PER_MINUTE": str(10 * (50 + SEQUENTIAL_REQUESTS + CONCURRENT_REQUESTS)),
             "PROXY_MAX_CONCURRENT": str(max(64, CONCURRENCY)),
             "PROXY_API_KEY": "", "OPENAI_API_KEY": ""}
    before_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    with tempfile.TemporaryDirectory(prefix="agenteval-bench-") as spool, \
            throwaway_database(admin_dsn) as dsn, \
            server("mock_upstream", "server:app", {"MOCK_LATENCY_MS": "0"}, label="fast-mock") as fast_mock, \
            server("mock_upstream", "server:app", {"MOCK_LATENCY_MS": str(UPSTREAM_LATENCY_MS)}, label="slow-mock") as slow_mock, \
            server("proxy", "main:app", {**local, "DATABASE_URL": dsn, "UPSTREAM_BASE": fast_mock, "UPSTREAM_API_KEY": "synthetic-only", "TRACE_SPOOL_PATH": str(Path(spool) / "fast.sqlite3")}, label="fast-proxy") as fast_proxy, \
            server("proxy", "main:app", {**local, "DATABASE_URL": dsn, "UPSTREAM_BASE": slow_mock, "UPSTREAM_API_KEY": "synthetic-only", "TRACE_SPOOL_PATH": str(Path(spool) / "slow.sqlite3")}, label="slow-proxy") as slow_proxy:

        direct = sequential(fast_mock, SEQUENTIAL_REQUESTS)
        proxied = sequential(fast_proxy, SEQUENTIAL_REQUESTS)
        direct_c = asyncio.run(concurrent(slow_mock, CONCURRENT_REQUESTS, CONCURRENCY))
        proxied_c = asyncio.run(concurrent(slow_proxy, CONCURRENT_REQUESTS, CONCURRENCY))
        expected = 50 + SEQUENTIAL_REQUESTS + CONCURRENT_REQUESTS
        acknowledged_ids = [*proxied.warmup_event_ids, *proxied.durable_event_ids, *proxied_c.durable_event_ids]
        accepted = len(acknowledged_ids)
        drain_started = time.monotonic()
        deadline = drain_started + DRAIN_SECONDS
        capture_drain_seconds = None
        queue_drained = False
        while True:
            recorded = count_traces(dsn)
            if recorded >= accepted and capture_drain_seconds is None:
                capture_drain_seconds = time.monotonic() - drain_started
            queue_metrics = {}
            for name,url in (("fast_proxy",fast_proxy), ("slow_proxy",slow_proxy)):
                try:
                    response = httpx.get(url + '/metrics', timeout=5)
                    response.raise_for_status()
                    queue_metrics[name] = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    queue_metrics[name] = {'metrics_error_category': type(exc).__name__}
            queue_drained = all(
                metrics.get('ingestion', {}).get('pending') == 0
                and metrics.get('ingestion', {}).get('leased') == 0
                and metrics.get('ingestion', {}).get('dead') == 0
                for metrics in queue_metrics.values())
            if (recorded >= accepted and queue_drained) or time.monotonic() >= deadline:
                break
            time.sleep(.2)
        drain_seconds = time.monotonic() - drain_started
        with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
            cur.execute('SELECT event_id FROM traces')
            persisted_ids = {row[0] for row in cur.fetchall()}
        missing_accepted = len(set(acknowledged_ids) - persisted_ids)
    after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)

    report = {'status': 'passed', 'traces_recorded': recorded, 'expected': expected,
              'durably_acknowledged_requests': accepted,
              'missing_durably_acknowledged_traces': missing_accepted,
              'trace_capture_rate': recorded / expected, 'accepted_trace_capture_rate':
                  (accepted - missing_accepted) / accepted if accepted else None,
              'drain_seconds': drain_seconds, 'capture_drain_seconds': capture_drain_seconds,
              'queue_drained': queue_drained,
              'upstream_latency_ms': UPSTREAM_LATENCY_MS, 'concurrency': CONCURRENCY,
              'sequential_requests': SEQUENTIAL_REQUESTS, 'concurrent_requests': CONCURRENT_REQUESTS,
              'sequential': {'direct': direct.summary(), 'proxy': proxied.summary()},
              'concurrent': {'direct': direct_c.summary(), 'proxy': proxied_c.summary()},
              'resource_usage': {'child_user_cpu_seconds': after_usage.ru_utime - before_usage.ru_utime,
                                 'child_system_cpu_seconds': after_usage.ru_stime - before_usage.ru_stime,
                                 'largest_child_peak_rss_kib': after_usage.ru_maxrss,
                                 'scope': 'Terminated child model/proxy processes only; excludes load generator, Postgres, Docker and other workloads.'},
              'proxy_metrics_before_shutdown': queue_metrics, 'provider_spend_usd': 0,
              'scope': 'Local synthetic model, metadata traces, SQLite durable queue with embedded ingestion. No paid judge or infrastructure cost included. Latencies describe successful requests; rejected-request timings are separate.',
              'machine': {'system': platform.system(), 'architecture': platform.machine(),
                          'visible_cpus': os.cpu_count(), 'cpu_quota_cores': cpu_quota_cores(),
                          'python': platform.python_version()}}
    total_failures = sum(result.failures for result in (direct,proxied,direct_c,proxied_c))
    report['failed_requests'] = total_failures
    if total_failures or recorded != expected or missing_accepted or not queue_drained:
        report['status'] = 'failed'
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if report['status'] != 'passed':
        sys.exit('Benchmark failed: rejections, transport errors, or capture mismatch; inspect JSON and retained service logs.')


if __name__ == "__main__":
    main()
