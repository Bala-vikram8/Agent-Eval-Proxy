"""Benchmark reporting must retain rejected calls and exclude them from successes."""
import sys

import httpx

from conftest import ROOT

sys.path.insert(0, str(ROOT))
from bench.overhead import LoadResult, percentile  # noqa: E402


def test_rejection_counts_and_diagnostics_do_not_expose_body():
    result = LoadResult()
    response = httpx.Response(503, json={"error": {
        "code": "trace_unavailable", "message": "synthetic-secret-value"}})
    result.observe(response, 42)
    summary = result.summary()
    assert summary["failed_requests"] == 1
    assert summary["status_counts"] == {"503": 1}
    assert summary["success_p50_ms"] is None
    assert summary["failure_p50_ms"] == 42
    assert summary["failure_examples"] == [{"status": "503", "code": "trace_unavailable", "warmup": False}]
    assert "synthetic-secret-value" not in str(summary)


def test_warmup_and_successful_capture_are_counted_separately():
    result = LoadResult()
    response = httpx.Response(200, headers={"x-trace-delivery": "durable", "x-trace-id": "synthetic-event"})
    result.observe(response, 10, warmup=True)
    result.observe(response, 20)
    result.elapsed = 2
    assert result.warmup_event_ids == ["synthetic-event"]
    assert result.durable_event_ids == ["synthetic-event"]
    assert result.summary()["successful_requests_per_second"] == .5
    assert result.summary()["success_p95_ms"] == 20
    assert result.failures == 0


def test_failure_examples_are_bounded_and_transport_errors_retained():
    result = LoadResult()
    for _ in range(20):
        result.observe(None, 5, failure_type="ReadTimeout")
    assert result.failures == 20
    assert len(result.failure_examples) == 10
    assert result.summary()["status_counts"] == {"transport_error": 20}
    assert percentile([], 95) is None
