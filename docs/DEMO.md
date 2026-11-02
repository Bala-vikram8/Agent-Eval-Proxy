# Simulated customer integration demonstration

This is a fictional automotive service-operations integration. It uses no real dealership, customer, vehicle, repair record or provider account. It is not evidence that Revion or any other real organization uses the project. The model and relevance judge are deterministic fixtures. No real repair advice is supplied.

## Customer workflow

A service coordinator asks where to route a work order whose parts order is on hold. The agent must retrieve fictional internal document `SIM-WO-7` through an allowlisted `lookup_workflow_document` tool. The document says to send the work order to the parts-review queue. A configuration fault makes the model falsely say to close the order and mark parts delivered.

The important integration boundary is the tool/document workflow. The proxy is added through the OpenAI client's base URL and agent label, without replacing the agent with a monitoring-specific implementation.

## Reproduce

Use Python 3.12, installed development requirements and the local Postgres administrator described in the README. The runner refuses non-loopback database hosts and creates/removes only a uniquely named synthetic database.

```bash
export TEST_DATABASE_URL=postgresql://agenteval:agenteval@127.0.0.1:5432/agenteval
python examples/automotive_demo.py --samples 60 --output artifacts/automotive
```

The runner:

1. Starts loopback model/judge fixtures, the actual proxy and evaluation worker, and a private temporary durable spool.
2. Sends independent synthetic work orders through retrieval/tool use and final model replies, producing a healthy baseline.
3. Introduces the fixture's explicit faulty configuration and sends another cohort.
4. Restores the configuration and sends a recovery cohort. Assertions require every recovered answer to match the operator reference.
5. Waits for actual persisted reference evaluations. Missing traces or unfinished evaluations fail the run.
6. Uses controlled synthetic timestamps to exercise completed monitoring windows immediately, rather than waiting 48 hours. It persists candidate deterioration alerts and checks duplicate-window deduplication and recovery behavior.
7. Renders the real Streamlit application headlessly, inspects trace/evaluation/alert data and exercises an explicitly enabled acknowledgement capability. This step uses synthetic-local mode; the separate authentication regression suite verifies the login boundary.
8. Saves `results.json` and logs, stops its processes and drops its temporary database.

The result file distinguishes measured local request latency and capture counts from synthetic token counts and judge scores. It does not claim provider performance, general factual accuracy or a deployed production recovery. Local compute/database expense is excluded from its zero mock-provider-spend field.

## Five-minute presentation narrative

- **Integration:** show the coordinator question, tool allowlist and fictional document. Explain the base-URL change and operator-controlled reference ID.
- **Normal operations:** show baseline reference scores and complete captured traces, then identify the relevant agent/model/config cohort.
- **Incident:** show the wrong routing instruction and failed reference result. Relevance alone may remain high; an answer can address a question while being wrong.
- **Investigation:** show the persisted alert, trace detail/evaluation history and operator acknowledgement. Explain the minimum-sample/confidence rule and its limitations.
- **Correction:** show the explicit configuration restoration, recovered answers, and the regression test `tests/test_automotive.py`.
- **Operating boundary:** show local validation results and remaining cloud/provider/recovery gates. Do not describe this synthetic exercise as customer adoption or production deployment.

## What this proves

Within the controlled fixture, actual application components can capture requests, evaluate against a configured reference, identify a known quality decline, render investigation data, record acknowledgement and detect the expected recovery. It does not prove the reference is true in a real domain, that all hallucinations are detected, that score changes are causal model drift, or that AWS networking/backups/alerts work.
