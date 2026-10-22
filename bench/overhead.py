"""Measures what the proxy adds on top of the upstream call.

Starts the mock upstream and the proxy as local processes against a throwaway Postgres
database, then sends the same chat completion directly to the mock and through the proxy.

  TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres python bench/overhead.py

Numbers depend on the machine, so compare runs on the same box.
"""
import asyncio
import contextlib
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
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
UPSTREAM_LATENCY_MS = 100  # stands in for model latency in the concurrent run


def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def server(app_dir: str, app: str, env: dict):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT / app_dir, env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            with contextlib.suppress(httpx.HTTPError):
                httpx.get(f"{url}/health" if app_dir == "proxy" else f"{url}/v1/models", timeout=1)
                break
            time.sleep(0.05)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


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


def percentile(values: list[float], p: float) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def sequential(url: str, n: int) -> list[float]:
    timings = []
    with httpx.Client(base_url=url, timeout=30) as client:
        for _ in range(50):  # warm up connections and code paths
            client.post("/v1/chat/completions", json=BODY)
        for _ in range(n):
            start = time.perf_counter()
            client.post("/v1/chat/completions", json=BODY).raise_for_status()
            timings.append((time.perf_counter() - start) * 1000)
    return timings


async def concurrent(url: str, n: int, concurrency: int) -> tuple[list[float], float]:
    timings: list[float] = []
    queue = iter(range(n))
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=60, limits=limits) as client:
        async def worker():
            for _ in queue:
                start = time.perf_counter()
                response = await client.post("/v1/chat/completions", json=BODY)
                response.raise_for_status()
                timings.append((time.perf_counter() - start) * 1000)
        began = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed = time.perf_counter() - began
    return timings, n / elapsed


def count_traces(dsn: str) -> int:
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM traces")
        return cur.fetchone()[0]


def main() -> None:
    admin_dsn = os.getenv("TEST_DATABASE_URL")
    if not admin_dsn:
        sys.exit("Set TEST_DATABASE_URL to a Postgres server this script may create a database on.")
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

    with throwaway_database(admin_dsn) as dsn, \
            server("mock_upstream", "server:app", {"MOCK_LATENCY_MS": "0"}) as fast_mock, \
            server("mock_upstream", "server:app", {"MOCK_LATENCY_MS": str(UPSTREAM_LATENCY_MS)}) as slow_mock, \
            server("proxy", "main:app", {"DATABASE_URL": dsn, "UPSTREAM_BASE": fast_mock, "UPSTREAM_API_KEY": "x"}) as fast_proxy, \
            server("proxy", "main:app", {"DATABASE_URL": dsn, "UPSTREAM_BASE": slow_mock, "UPSTREAM_API_KEY": "x"}) as slow_proxy:

        direct = sequential(fast_mock, SEQUENTIAL_REQUESTS)
        proxied = sequential(fast_proxy, SEQUENTIAL_REQUESTS)
        direct_c, direct_rps = asyncio.run(concurrent(slow_mock, CONCURRENT_REQUESTS, CONCURRENCY))
        proxied_c, proxied_rps = asyncio.run(concurrent(slow_proxy, CONCURRENT_REQUESTS, CONCURRENCY))
        time.sleep(1)
        expected = 50 + SEQUENTIAL_REQUESTS + CONCURRENT_REQUESTS  # proxied warm up, sequential, concurrent
        recorded = count_traces(dsn)

    rows = [
        ("Sequential, upstream answers instantly", direct, proxied, None, None),
        (f"{CONCURRENCY} concurrent clients, upstream takes {UPSTREAM_LATENCY_MS} ms", direct_c, proxied_c,
         direct_rps, proxied_rps),
    ]
    print(f"Machine: {platform.system()} {platform.machine()}, {os.cpu_count()} CPUs, Python {platform.python_version()}\n")
    print("| Scenario | Direct p50 / p95 | Through proxy p50 / p95 | Added at p50 / p95 | Throughput direct / proxy |")
    print("|---|---|---|---|---|")
    for name, a, b, rps_a, rps_b in rows:
        p50a, p95a, p50b, p95b = percentile(a, 50), percentile(a, 95), percentile(b, 50), percentile(b, 95)
        throughput = f"{rps_a:,.0f} / {rps_b:,.0f} req/s" if rps_a else "n/a"
        print(f"| {name} | {p50a:.1f} / {p95a:.1f} ms | {p50b:.1f} / {p95b:.1f} ms "
              f"| {p50b - p50a:+.1f} / {p95b - p95a:+.1f} ms | {throughput} |")
    print(f"\nTraces recorded: {recorded} of {expected} proxied requests")
    print(json.dumps({"traces_recorded": recorded, "expected": expected}))


if __name__ == "__main__":
    main()
