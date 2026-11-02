"""Synthetic customer integration regression; no actual service/dealership data."""
import sys

import httpx
from openai import OpenAI
import pytest

from conftest import ROOT, free_port, run_server

sys.path.insert(0, str(ROOT))
from examples.automotive_demo import TOOLS, lookup_document  # noqa: E402
from examples.automotive_upstream import BROKEN, EXPECTED, MODEL  # noqa: E402


@pytest.mark.parametrize("name,args", [
    ("delete_work_order", '{"document_id":"SIM-WO-7"}'),
    ("lookup_workflow_document", "{"),
    ("lookup_workflow_document", '{"document_id":"real-customer-document"}'),
    ("lookup_workflow_document", '{"document_id":"SIM-WO-7","extra":true}'),
])
def test_tool_dispatch_rejects_unapproved_actions(name, args):
    with pytest.raises(ValueError):
        lookup_document(name, args)


def test_simulated_routing_incident_is_fixed_by_configuration():
    with run_server("examples", "automotive_upstream:app", free_port(), {}, "/health") as url:
        client = OpenAI(base_url=url + "/v1", api_key="synthetic-only", max_retries=0)
        messages = [{"role": "user", "content": "SIMULATED parts order is on hold. Which queue receives it?"}]
        first = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        [call] = first.choices[0].message.tool_calls
        tool_result = lookup_document(call.function.name, call.function.arguments)
        messages.extend([{"role": "assistant", "content": None, "tool_calls": [{
            "id": call.id, "type": "function", "function": {"name": call.function.name,
            "arguments": call.function.arguments}}]}, {"role": "tool", "tool_call_id": call.id, "content": tool_result}])
        for broken, expected in [(False, EXPECTED), (True, BROKEN), (False, EXPECTED)]:
            httpx.post(url + "/scenario/config", json={"broken": broken}).raise_for_status()
            answer = client.chat.completions.create(model=MODEL, messages=messages).choices[0].message.content
            assert answer == expected
        chunks = client.chat.completions.create(model=MODEL, messages=messages, stream=True)
        assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == EXPECTED
        client.close()
