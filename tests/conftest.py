"""Shared fixtures. Tests run the real proxy and mock upstream as subprocesses against a
throwaway Postgres database created from TEST_DATABASE_URL."""
import contextlib
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import psycopg2
import pytest
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")


def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_up(url: str, proc: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited early:\n{proc.stdout.read()}")
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


# Settings each test passes explicitly, never inherited from the developer's shell.
CONTROLLED_ENV = {"OPENAI_API_KEY", "UPSTREAM_API_KEY", "UPSTREAM_BASE", "PROXY_API_KEY", "DATABASE_URL",
                  "JUDGE_API_KEY", "JUDGE_BASE_URL", "JUDGE_MODEL"}


@contextlib.contextmanager
def run_server(app_dir: str, app: str, port: int, env: dict, health_path: str):
    base_env = {k: v for k, v in os.environ.items() if k not in CONTROLLED_ENV}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT / app_dir, env={**base_env, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_until_up(f"http://127.0.0.1:{port}{health_path}", proc)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def apply_schema(dsn: str, path: Path = ROOT / "sql" / "init.sql") -> None:
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(path.read_text())


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set TEST_DATABASE_URL to a Postgres server the tests may create databases on")
    return dsn


@pytest.fixture(scope="session")
def make_database(admin_dsn):
    """Factory for empty throwaway databases, dropped at the end of the session."""
    created = []

    def make() -> str:
        name = f"agenteval_test_{uuid.uuid4().hex[:10]}"
        with contextlib.closing(psycopg2.connect(admin_dsn)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return psycopg2.extensions.make_dsn(admin_dsn, dbname=name)

    yield make
    with contextlib.closing(psycopg2.connect(admin_dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for name in created:
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def database(make_database) -> str:
    dsn = make_database()
    apply_schema(dsn)
    return dsn


@pytest.fixture
def db(database):
    """Empty traces table for each test."""
    with contextlib.closing(psycopg2.connect(database)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("TRUNCATE traces RESTART IDENTITY")
    return database


def fetch(dsn: str, query: str, params=None) -> list[dict]:
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def wait_for_traces(dsn: str, count: int, timeout: float = 10) -> list[dict]:
    """Trace writes are asynchronous, so poll until they land."""
    deadline = time.time() + timeout
    while True:
        rows = fetch(dsn, "SELECT * FROM traces ORDER BY id")
        if len(rows) >= count or time.time() > deadline:
            return rows
        time.sleep(0.05)


@pytest.fixture(scope="session")
def mock_upstream() -> str:
    with run_server("mock_upstream", "server:app", free_port(), {"MOCK_STREAM_DELAY_MS": "100"}, "/v1/models") as url:
        yield url


@pytest.fixture(scope="session")
def start_proxy():
    """Factory: start a proxy with the given environment, stopped at session end."""
    stack = contextlib.ExitStack()

    def start(**env) -> str:
        env = {k: v for k, v in env.items() if v is not None}
        return stack.enter_context(run_server("proxy", "main:app", free_port(), env, "/health"))

    with stack:
        yield start


@pytest.fixture(scope="session")
def proxy(start_proxy, database, mock_upstream) -> str:
    return start_proxy(DATABASE_URL=database, UPSTREAM_BASE=mock_upstream, UPSTREAM_API_KEY="upstream-key")
