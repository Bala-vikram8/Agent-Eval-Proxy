"""Support triage agent that sends realistic traffic through the proxy.

Each ticket runs through a small LangGraph pipeline:
  triage       one plain chat completion that classifies the ticket
  investigate  the model may call a lookup_account tool, then answers with the tool result
  reply        a streamed reply to the customer
The run ends with one request to a model that does not exist, so error tracking has
something to show. The agent code knows nothing about the proxy except its URL.
"""
import json
import os
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI, OpenAIError

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8000/v1")
MODEL = os.getenv("DEMO_MODEL", "gpt-4o-mini")
# The proxy adds the real upstream key. Send PROXY_API_KEY only if the proxy requires one.
client = OpenAI(base_url=PROXY_URL, api_key=os.getenv("PROXY_API_KEY") or "unused")

TICKETS = [
    "User cannot login, password reset email not arriving, critical for payroll.",
    "How do I change my profile picture?",
    "Billing discrepancy of $12.50 on last invoice for account 4471.",
]

LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_account",
        "description": "Look up a customer's billing account and recent charges.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Account number or customer name"}},
            "required": ["query"],
        },
    },
}


class State(TypedDict, total=False):
    ticket: str
    priority: str
    findings: str
    reply: str


def lookup_account(query: str) -> str:
    """Stand in for a real billing system."""
    return json.dumps({"query": query, "last_invoice": "$112.50", "expected": "$100.00",
                       "note": "duplicate service fee of $12.50"})


def triage(state: State) -> State:
    result = client.chat.completions.create(model=MODEL, messages=[
        {"role": "system", "content": "You are a support triage agent. Classify the ticket as urgent, "
                                      "normal, or low and give the next action in one or two sentences."},
        {"role": "user", "content": state["ticket"]},
    ])
    return {"priority": result.choices[0].message.content or ""}


def investigate(state: State) -> State:
    messages = [
        {"role": "system", "content": "You are a support agent. Call lookup_account when the ticket is "
                                      "about billing or charges, then say what you found in one sentence. "
                                      "Otherwise answer the ticket briefly."},
        {"role": "user", "content": state["ticket"]},
    ]
    first = client.chat.completions.create(model=MODEL, messages=messages, tools=[LOOKUP_TOOL])
    message = first.choices[0].message
    if not message.tool_calls:
        return {"findings": message.content or ""}

    messages.append({"role": "assistant", "content": message.content, "tool_calls": [
        {"id": call.id, "type": "function",
         "function": {"name": call.function.name, "arguments": call.function.arguments}}
        for call in message.tool_calls
    ]})
    for call in message.tool_calls:
        arguments = json.loads(call.function.arguments or "{}")
        messages.append({"role": "tool", "tool_call_id": call.id,
                         "content": lookup_account(str(arguments.get("query", "")))})
    final = client.chat.completions.create(model=MODEL, messages=messages, tools=[LOOKUP_TOOL])
    return {"findings": final.choices[0].message.content or ""}


def reply(state: State) -> State:
    stream = client.chat.completions.create(model=MODEL, stream=True, messages=[
        {"role": "system", "content": "Write a two sentence reply to the customer. Be specific about the next step."},
        {"role": "user", "content": f"Ticket: {state['ticket']}\nTriage: {state['priority']}\n"
                                    f"Findings: {state['findings']}"},
    ])
    text = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
    return {"reply": text}


graph = StateGraph(State)
graph.add_node("triage", triage)
graph.add_node("investigate", investigate)
graph.add_node("reply", reply)
graph.add_edge(START, "triage")
graph.add_edge("triage", "investigate")
graph.add_edge("investigate", "reply")
graph.add_edge("reply", END)
agent = graph.compile()


if __name__ == "__main__":
    print(f"Sending traffic through {PROXY_URL} with model {MODEL}\n")
    for ticket in TICKETS:
        result = agent.invoke({"ticket": ticket})
        print(f"Ticket: {ticket}\nTriage: {result['priority']}\nFindings: {result['findings']}\n"
              f"Reply: {result['reply']}\n")
    try:
        client.chat.completions.create(model="gpt-does-not-exist",
                                       messages=[{"role": "user", "content": "ping"}])
    except OpenAIError as exc:
        print(f"Expected error, recorded by the proxy: {type(exc).__name__}")
    print("Done. Dashboard: http://localhost:8501")
