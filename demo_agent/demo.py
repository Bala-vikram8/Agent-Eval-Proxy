import os
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
import operator
from openai import OpenAI

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8000/v1")
client = OpenAI(base_url=PROXY_URL, api_key="proxy")  # key ignored, proxy injects

class State(TypedDict):
    messages: Annotated[list, operator.add]
    ticket: str

def triage(state: State):
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a support triage agent. Classify the ticket as urgent, normal, or low and suggest next action."},
            {"role": "user", "content": state["ticket"]}
        ]
    )
    return {"messages": [response.choices[0].message.content]}

workflow = StateGraph(State)
workflow.add_node("triage", triage)
workflow.set_entry_point("triage")
workflow.add_edge("triage", END)
app = workflow.compile()

if __name__ == "__main__":
    tickets = [
        "User cannot login, password reset email not arriving, critical for payroll.",
        "How do I change my profile picture?",
        "Billing discrepancy of $12.50 on last invoice."
    ]
    for t in tickets:
        result = app.invoke({"ticket": t, "messages": []})
        print(f"Ticket: {t}")
        print(f"Result: {result['messages'][-1]}\n")
