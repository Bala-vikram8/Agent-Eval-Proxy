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


def test_crlf_delimiters_split_at_every_byte_boundary():
    raw = (event(chunk({'content': 'hello'}, 'stop')) + b'data: [DONE]\n\n').replace(b'\n', b'\r\n')
    for split in range(1, len(raw)):
        acc = StreamAccumulator()
        out = acc.feed(raw[:split], 0) + acc.feed(raw[split:], 1)
        assert acc.response()['choices'][0]['message']['content'] == 'hello'
        assert acc.done
        assert b''.join(out) == raw.replace(b'\r\n', b'\n')


def test_sse_event_size_limit_without_delimiter():
    import pytest
    from streaming import StreamLimitError
    acc = StreamAccumulator(max_event_bytes=32)
    with pytest.raises(StreamLimitError):
        acc.feed(b'data: ' + b'x' * 33, 0)


def test_capture_limit_does_not_change_forwarded_completion():
    raw = event(chunk({'content': 'long synthetic text'}, 'stop')) + b'data: [DONE]\n\n'
    acc = StreamAccumulator(max_capture_bytes=4)
    assert b''.join(acc.feed(raw, 0)) == raw
    assert acc.done and acc.capture_truncated
    assert acc.response()['choices'][0]['message']['content'] is None


def test_malformed_provider_shapes_do_not_crash_parser():
    acc = StreamAccumulator()
    for malformed in [{'choices': [None]}, {'choices': 'wrong'},
                      {'choices': [{'delta': {'content': {}, 'tool_calls': [None, {'index': []}]}}]}]:
        assert acc.feed(event(malformed), 0) == [event(malformed)]


def test_deeply_nested_provider_json_is_marked_malformed_without_crashing():
    raw = b'data: ' + b'[' * 1200 + b'0' + b']' * 1200 + b'\n\n'
    acc = StreamAccumulator()
    assert acc.feed(raw, 0) == [raw]
    assert acc.malformed and not acc.done
