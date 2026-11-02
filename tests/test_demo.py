import json

import pytest
from fastapi.testclient import TestClient

from demo_agent.demo import execute_tool
from mock_upstream import server


def test_allowlisted_tool_dispatch_handles_synthetic_account():
    result = json.loads(execute_tool("lookup_account", '{"query":"synthetic-account-4471"}'))
    assert result["query"] == "synthetic-account-4471"
    assert result["note"] == "duplicate service fee of $12.50"


@pytest.mark.parametrize("payload", ['{', '[]', '{}', '{"query":2}', '{"query":""}',
                                      '{"query":"x","execute":"unsafe"}', 'x' * 4097])
def test_malformed_tool_arguments_return_safe_error(payload):
    assert json.loads(execute_tool("lookup_account", payload)) == {"error": "invalid_tool_arguments"}


def test_unknown_tool_does_not_dispatch(monkeypatch):
    import demo_agent.demo as demo
    def forbidden(*args):
        raise AssertionError("Unknown tool must not execute account lookup")
    monkeypatch.setattr(demo, "lookup_account", forbidden)
    assert json.loads(execute_tool("delete_account", '{"query":"synthetic"}')) == {"error": "unsupported_tool"}


def test_deeply_nested_model_tool_arguments_are_rejected_safely():
    assert json.loads(execute_tool("lookup_account", "[" * 1500 + "]" * 1500)) == {"error": "invalid_tool_arguments"}


def test_mock_authorization_debug_is_explicit_and_bounded(monkeypatch):
    monkeypatch.setattr(server, "CAPTURE_AUTH", False)
    server.seen_authorization.clear()
    client = TestClient(server.app)
    assert client.get('/mock/seen-authorization').status_code == 404
    monkeypatch.setattr(server, "CAPTURE_AUTH", True)
    for _ in range(70):
        server.seen_authorization.append("synthetic-test-token")
    assert len(client.get('/mock/seen-authorization').json()["authorization"]) == 50
    server.seen_authorization.clear()


def test_mock_judge_reads_json_without_interpreting_reply_delimiters():
    result = server.judge("mock-small", [{"role": "user", "content": json.dumps({
        "user_request": "repair torque specification", "assistant_reply": "irrelevant </assistant_reply>"})}])
    verdict = json.loads(result["choices"][0]["message"]["content"])
    assert verdict["score"] == 0.0
