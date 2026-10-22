"""Unit tests for the stream parser used by the proxy."""
import json
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT / "proxy"))
from streaming import StreamAccumulator  # noqa: E402


def event(payload) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def chunk(delta, finish=None):
    return {"id": "c1", "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def test_events_split_across_reads_are_reassembled():
    raw = event(chunk({"content": "Hello "})) + event(chunk({"content": "world"}, "stop")) + b"data: [DONE]\n\n"
    acc, forwarded = StreamAccumulator(), []
    for i in range(0, len(raw), 7):  # feed in awkward 7 byte pieces
        forwarded += acc.feed(raw[i:i + 7], now=float(i))
    assert b"".join(forwarded) == raw
    message = acc.response()["choices"][0]["message"]
    assert message["content"] == "Hello world"
    assert acc.done and acc.response()["choices"][0]["finish_reason"] == "stop"


def test_usage_chunk_hidden_only_when_asked():
    usage = {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    hidden = StreamAccumulator(hide_usage_chunk=True)
    assert hidden.feed(event(usage), now=0) == []
    assert hidden.usage["total_tokens"] == 5
    shown = StreamAccumulator(hide_usage_chunk=False)
    assert shown.feed(event(usage), now=0) == [event(usage)]


def test_crlf_and_comments_pass_through():
    acc = StreamAccumulator()
    out = acc.feed(b": keep alive\r\n\r\n" + event(chunk({"content": "x"})).replace(b"\n", b"\r\n"), now=1.5)
    assert out[0] == b": keep alive\n\n"
    assert acc.first_token_at == 1.5


def test_tool_call_deltas_are_merged():
    acc = StreamAccumulator()
    acc.feed(event(chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                          "function": {"name": "lookup", "arguments": ""}}]})), now=0)
    acc.feed(event(chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"id": '}}]})), now=0)
    acc.feed(event(chunk({"tool_calls": [{"index": 0, "function": {"arguments": "42}"}}]}, "tool_calls")), now=0)
    [call] = acc.tool_calls
    assert call["id"] == "call_1" and call["function"] == {"name": "lookup", "arguments": '{"id": 42}'}
    assert acc.response()["choices"][0]["message"]["content"] is None
