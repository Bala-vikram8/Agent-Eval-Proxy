"""End to end tests: client -> proxy subprocess -> mock upstream subprocess -> Postgres."""
import json
import time

import httpx
import openai
import pytest

from conftest import fetch, free_port, wait_for_traces

TICKET = [{"role": "user", "content": "User cannot login, password reset email not arriving."}]
BILLING = [{"role": "user", "content": "Billing discrepancy on my last invoice for account 4471."}]
TOOL = {"type": "function", "function": {"name": "lookup_account", "parameters": {"type": "object", "properties": {}}}}


def client_for(proxy_url: str, key: str = "unused") -> openai.OpenAI:
    return openai.OpenAI(base_url=f"{proxy_url}/v1", api_key=key, max_retries=0)


def sse_events(response: httpx.Response) -> list[tuple[float, str]]:
    """Read a streamed response and return (arrival time, data payload) per event."""
    events, buffer = [], ""
    for text in response.iter_text():
        buffer += text
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            if event.startswith("data: "):
                events.append((time.perf_counter(), event[6:]))
    return events


def test_completion_is_forwarded_and_logged(proxy, db):
    reply = client_for(proxy).chat.completions.create(model="gpt-4o-mini", messages=TICKET)
    assert reply.choices[0].message.content.startswith("Priority: urgent")

    [row] = wait_for_traces(db, 1)
    assert row["model"] == "gpt-4o-mini"
    assert row["status_code"] == 200
    assert row["streamed"] is False
    assert row["messages"] == TICKET
    assert row["response"]["choices"][0]["message"]["content"] == reply.choices[0].message.content
    assert row["prompt_tokens"] == reply.usage.prompt_tokens
    assert row["completion_tokens"] == reply.usage.completion_tokens
    assert row["latency_ms"] >= 0
    assert row["eval_status"] == "pending"


def test_upstream_error_is_returned_and_logged(proxy, db):
    with pytest.raises(openai.NotFoundError):
        client_for(proxy).chat.completions.create(model="gpt-does-not-exist", messages=TICKET)
    [row] = wait_for_traces(db, 1)
    assert row["status_code"] == 404
    assert row["response"]["error"]["code"] == "model_not_found"
    assert row["prompt_tokens"] is None


def test_stream_reaches_client_incrementally(proxy, db):
    """The mock waits 100 ms between chunks; the first chunk must not wait for the last."""
    body = {"model": "gpt-4o-mini", "messages": TICKET, "stream": True}
    start = time.perf_counter()
    with httpx.stream("POST", f"{proxy}/v1/chat/completions", json=body, timeout=30) as response:
        assert response.status_code == 200
        events = sse_events(response)
    end = time.perf_counter()

    payloads = [json.loads(data) for _, data in events if data != "[DONE]"]
    first_content_at = next(t for (t, data) in events
                            if data != "[DONE]" and json.loads(data)["choices"][0]["delta"].get("content"))
    assert end - start > 0.8, "mock should take about a second to stream this reply"
    assert first_content_at - start < (end - start) / 2, "proxy buffered the stream"
    # The proxy asked upstream for usage, but the caller did not, so it must not see that chunk.
    assert all(p["choices"] for p in payloads)
    assert events[-1][1] == "[DONE]"

    streamed_text = "".join(p["choices"][0]["delta"].get("content") or "" for p in payloads)
    [row] = wait_for_traces(db, 1)
    assert row["streamed"] is True
    assert row["response"]["choices"][0]["message"]["content"] == streamed_text
    assert row["response"]["stream_completed"] is True
    assert row["prompt_tokens"] > 0 and row["completion_tokens"] > 0
    assert 0 <= row["ttft_ms"] < row["latency_ms"]


def test_usage_chunk_is_kept_when_caller_asks_for_it(proxy, db):
    stream = client_for(proxy).chat.completions.create(
        model="gpt-4o-mini", messages=TICKET, stream=True, stream_options={"include_usage": True})
    chunks = list(stream)
    assert chunks[-1].choices == [] and chunks[-1].usage.total_tokens > 0
    [row] = wait_for_traces(db, 1)
    assert row["total_tokens"] == chunks[-1].usage.total_tokens


def test_streamed_tool_call_is_rebuilt(proxy, db):
    stream = client_for(proxy).chat.completions.create(
        model="gpt-4o-mini", messages=BILLING, tools=[TOOL], stream=True)
    list(stream)
    [row] = wait_for_traces(db, 1)
    [call] = row["tool_calls"]
    assert call["function"]["name"] == "lookup_account"
    assert "account 4471" in json.loads(call["function"]["arguments"])["query"]
    assert row["response"]["choices"][0]["finish_reason"] == "tool_calls"


def test_other_endpoints_are_forwarded_but_not_logged(proxy, db):
    models = client_for(proxy).models.list()
    assert "gpt-4o-mini" in [m.id for m in models.data]
    time.sleep(0.3)
    assert fetch(db, "SELECT count(*) AS n FROM traces")[0]["n"] == 0


def test_proxy_key_is_enforced_and_replaced(start_proxy, database, mock_upstream, db):
    url = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=mock_upstream,
                      UPSTREAM_API_KEY="upstream-key", PROXY_API_KEY="team-secret")
    with pytest.raises(openai.AuthenticationError):
        client_for(url, key="wrong").chat.completions.create(model="gpt-4o-mini", messages=TICKET)
    client_for(url, key="team-secret").chat.completions.create(model="gpt-4o-mini", messages=TICKET)

    seen = httpx.get(f"{mock_upstream}/mock/seen-authorization").json()["authorization"]
    assert seen[-1] == "Bearer upstream-key"
    assert "Bearer wrong" not in seen and "Bearer team-secret" not in seen
    assert len(wait_for_traces(db, 1)) == 1  # the rejected call never reached upstream


def test_openai_key_is_never_sent_to_another_upstream(start_proxy, database, mock_upstream, db):
    """With no UPSTREAM_API_KEY, the caller's own key is passed through, and OPENAI_API_KEY
    is ignored because the upstream is not api.openai.com."""
    url = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=mock_upstream, OPENAI_API_KEY="sk-real-openai-key")
    client_for(url, key="callers-own-key").chat.completions.create(model="gpt-4o-mini", messages=TICKET)
    seen = httpx.get(f"{mock_upstream}/mock/seen-authorization").json()["authorization"]
    assert seen[-1] == "Bearer callers-own-key"
    assert "Bearer sk-real-openai-key" not in seen


def test_unreachable_upstream_gives_502_and_is_logged(start_proxy, database, db):
    url = start_proxy(DATABASE_URL=database, UPSTREAM_BASE=f"http://127.0.0.1:{free_port()}")
    response = httpx.post(f"{url}/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": TICKET})
    assert response.status_code == 502
    [row] = wait_for_traces(db, 1)
    assert row["status_code"] == 502 and "ConnectError" in row["response"]["proxy_error"]


def test_database_outage_does_not_break_requests(start_proxy, mock_upstream):
    url = start_proxy(DATABASE_URL=f"postgresql://nobody@127.0.0.1:{free_port()}/none",
                      UPSTREAM_BASE=mock_upstream, UPSTREAM_API_KEY="upstream-key")
    for _ in range(3):
        reply = client_for(url).chat.completions.create(model="gpt-4o-mini", messages=TICKET)
        assert reply.choices[0].message.content
    assert httpx.get(f"{url}/health").json() == {"status": "ok"}


def test_client_disconnect_mid_stream_is_still_logged(proxy, db):
    body = {"model": "gpt-4o-mini", "messages": TICKET, "stream": True}
    with httpx.stream("POST", f"{proxy}/v1/chat/completions", json=body, timeout=30) as response:
        next(response.iter_raw())  # read the first bytes, then hang up
    [row] = wait_for_traces(db, 1)
    assert row["streamed"] is True
    assert row["response"]["stream_completed"] is False
