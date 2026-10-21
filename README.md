# AgentEval Proxy

Drop-in proxy that captures every LLM call, logs latency and cost, runs automatic faithfulness scoring, and shows the results on one screen. Built so a Forward Deployed Engineer can instrument an existing agent without rewriting the customer's stack.

## Problem
Production agent teams cannot tell whether their system is helping or just burning tokens. Most observability tools require code changes or lock you into one provider. This proxy sits in front of any OpenAI-compatible endpoint and works with the agent as-is.

## What it does
- Forwards requests to the real provider and logs the full trace (messages, response, tokens, latency, rough cost).
- Stores traces in Postgres.
- Background worker scores faithfulness with an LLM-as-judge and checks tool-call presence.
- Streamlit dashboard shows volume, latency, cost, average faithfulness, and low-score examples.

## Measured results (local run)
- 11 traces captured
- Average latency 1197 ms
- Total cost $0.0003
- Average faithfulness 0.75
- Tool correctness rate 100% (presence check only)

## Example trace
Request: classify support ticket "User cannot login, password reset email not arriving, critical for payroll"
Response: Classification: Urgent. Next action: escalate to IT support immediately and inform the user their issue is being prioritized.
Logged: 1397 ms, 18 tokens, $0.00004

## Example eval score
Trace 9 scored 0.00 faithfulness. Reason: the response did not address the user's question about changing a profile picture and instead returned an unrelated credit-balance error. That trace came from a run where the API key had no credits, which the dashboard surfaces as a low-score example instead of hiding it.

## Known limitations
- Tool-correctness check is a stub that only verifies a tool call occurred.
- No streaming support, no multi-provider auth, no real failure clustering.
- Cost estimate is a fixed gpt-4o-mini rate, not live pricing.

## Quick start
1. Copy `.env.example` to `.env` and add an OpenAI key with credits.
2. `docker compose up --build`
3. Point any client at `http://localhost:8000/v1`.
4. Dashboard at `http://localhost:8501`.
5. Generate sample traces: `docker compose --profile demo run --rm demo`.

MIT License.