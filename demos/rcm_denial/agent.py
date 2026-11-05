"""Denial investigation agent for the synthetic revenue cycle scenario.

A LangGraph loop: the model calls tools to read the claim, the remittance and the payer
policy, then writes a recommendation. Every model call goes through the AgentEval proxy with
two extra headers, which the proxy reads and never forwards upstream:

  X-AgentEval-Case-Id: <claim id>          ties the traces to the business case
  X-AgentEval-Evaluations: grounded        asks for evidence grounded evaluation

The agent knows nothing else about the proxy. With a real model the recommendation is
whatever the model writes; in offline mode the mock upstream plays the model from
offline_script.json, so the outcomes there are scripted on purpose.
"""
import json
import os
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI

import data

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8000/v1")
MODEL = os.getenv("DEMO_MODEL", "gpt-4o-mini")
MAX_TOOL_ROUNDS = 4
client = OpenAI(base_url=PROXY_URL, api_key=os.getenv("PROXY_API_KEY") or "unused", max_retries=1)

SYSTEM = """You are a denial investigation assistant for a healthcare revenue cycle team. All data is synthetic.
For the claim in the request: call get_claim, get_remittance and get_payer_policy (use the payer_id and CPT code from the claim).
Then write a recommendation of at most four sentences: the denial reason with its code, the relevant facts, and one next action
(corrected claim, appeal, write-off review, or escalate to a person). Cite the policy id and version.
State only facts that appear in the tool results. If a document is missing, say so and escalate instead of guessing."""

TOOLS = [
    {"type": "function", "function": {
        "name": "get_claim", "description": "Read a claim by id.",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "get_remittance", "description": "Read the remittance advice (835) for a claim.",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "get_payer_policy", "description": "Look up the payer policy for a CPT code.",
        "parameters": {"type": "object", "properties": {"payer_id": {"type": "string"}, "cpt": {"type": "string"}},
                       "required": ["payer_id", "cpt"]}}},
]
TOOL_FUNCTIONS = {"get_claim": data.get_claim, "get_remittance": data.get_remittance,
                  "get_payer_policy": data.get_payer_policy}


class State(TypedDict, total=False):
    claim_id: str
    messages: list
    rounds: int
    recommendation: str
    trace_ids: list


def call_model(state: State) -> State:
    raw = client.chat.completions.with_raw_response.create(
        model=MODEL, messages=state["messages"], tools=TOOLS, temperature=0,
        extra_headers={"X-AgentEval-Case-Id": state["claim_id"], "X-AgentEval-Evaluations": "grounded"},
    )
    reply = raw.parse()
    message = reply.choices[0].message
    entry = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        entry["tool_calls"] = [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in message.tool_calls]
    trace_ids = state.get("trace_ids", []) + [raw.headers.get("x-agenteval-trace-id")]
    return {"messages": state["messages"] + [entry], "trace_ids": trace_ids,
            "recommendation": message.content or "", "rounds": state.get("rounds", 0) + 1}


def run_tools(state: State) -> State:
    results = []
    for call in state["messages"][-1]["tool_calls"]:
        function = TOOL_FUNCTIONS.get(call["function"]["name"])
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            output = function(**args) if function else {"error": "unknown tool"}
        except (TypeError, ValueError) as exc:
            output = {"error": f"bad tool arguments: {exc}"}
        results.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output)})
    return {"messages": state["messages"] + results}


def next_step(state: State) -> str:
    wants_tools = bool(state["messages"][-1].get("tool_calls"))
    return "tools" if wants_tools and state["rounds"] < MAX_TOOL_ROUNDS else END


graph = StateGraph(State)
graph.add_node("model", call_model)
graph.add_node("tools", run_tools)
graph.add_edge(START, "model")
graph.add_conditional_edges("model", next_step, {"tools": "tools", END: END})
graph.add_edge("tools", "model")
agent = graph.compile()


def investigate(claim_id: str) -> State:
    return agent.invoke({"claim_id": claim_id, "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Investigate the denial on claim {claim_id} and recommend the next action."},
    ]})
