# 90 second CTO demo

Everything runs offline on a laptop: a mock model, a stub judge, and synthetic data from a fictional payer. Say that up front. The point of the demo is the architecture and the failure handling, not the judge's accuracy.

## Before recording

Run these once, off camera.

```bash
# Starts from an empty database. `down -v` deletes the local Docker volumes for this project
# (Postgres data and the trace spool). Skip it if you want to keep earlier traces.
docker compose -f docker-compose.yml -f docker-compose.offline.yml --profile demo --profile rcm down -v
make offline
until curl -fs http://127.0.0.1:8000/health/ready > /dev/null; do sleep 1; done
```

Arrange the screen with a terminal on the left and the dashboard (http://localhost:8501) on the right. Use a terminal font size of 16 or more. Keep the second terminal tab ready for the outage step.

## Recording sequence

| Time | Screen | Do | Say |
|---|---|---|---|
| 0:00 | Dashboard, empty | Nothing | "This is AgentEval, a proxy between an AI agent and its model. The agent changes one line, its base URL. Everything here is synthetic and offline." |
| 0:08 | Terminal | `make offline-rcm-demo` | "A LangGraph agent investigates three synthetic claim denials. It calls tools for the claim, the remit and the payer policy, then recommends a next action." |
| 0:20 | Terminal output | Point at CLM-24-10047 | "This one says 180 days and a contracted rate of $430. The policy it retrieved says 90 days, and $430 appears nowhere in its evidence." |
| 0:30 | Terminal output | Point at the verdict lines | "After the reply went back, the worker checked every claim against the tool results. The first case is supported. The second is unsupported, and the third had no policy to cite, so it is insufficient evidence. Both went to review." |
| 0:40 | Dashboard, Human review | Open review for CLM-24-10047 | "The reviewer sees why it was flagged, the reply, and the evidence with contact details already redacted. This is retrospective: the agent's answer was not blocked." |
| 0:50 | Dashboard | Type a name, choose Reject, note "rate not in evidence", submit | "Every decision goes into an append-only audit trail. If two reviewers act at once, the second gets a conflict instead of overwriting." |
| 0:58 | Second terminal | `$C stop postgres` then `$C --profile demo run --rm --no-deps demo` | "Now the database goes down while traffic keeps flowing." |
| 1:08 | Terminal | `curl -s localhost:8000/health/ready` | "Requests still succeed. Readiness says degraded, and `spool_depth` shows 11 traces waiting in a durable local spool." |
| 1:14 | Terminal | `$C start postgres`, wait 5 s, refresh dashboard | "When Postgres comes back, the spool drains. Every trace arrives exactly once, because each one has an idempotency key." |
| 1:22 | Dashboard, top | Point at traces, cost, p95 ingestion delay | "Cost is computed from the model the provider actually served. The limits are in the README: the judge has not been measured with a real model yet, and this is not built for real patient data." |
| 1:30 | | Stop | |

Set `C` in the second terminal before recording:

```bash
C="docker compose -f docker-compose.yml -f docker-compose.offline.yml"
```

`--no-deps` matters. Without it, `docker compose run` starts the demo's dependencies, which include Postgres, and quietly ends the outage. Rehearsed on the offline stack: with Postgres stopped, the demo agent completed, readiness reported `degraded` with 11 traces spooled, and after `start postgres` the trace count rose from 23 to 34.

## If something goes wrong on camera

* The review queue is empty: the worker polls every 5 seconds; refresh after a few seconds.
* The dashboard shows an error while Postgres is stopped: expected, it reads from Postgres. Refresh once it is back.
* As a fallback for the outage step, one request is enough:

  ```bash
  curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello during outage"}]}'
  ```

## Claims to avoid

* Do not quote an accuracy number for the grounding judge. With the mock stub, the numbers describe the stub, and the real judge has not been run against the dataset.
* Do not say it prevents bad answers. Every check runs after the reply has been returned.
* Do not say HIPAA, compliant, or production ready.

## Rehearsed answers

* **"How good is the judge?"** "Unknown with a real model. The deterministic layer alone catches 8 of 21 labeled cases and misses anything that needs reading comprehension. The next step is running the dataset with a real judge and getting it labeled independently."
* **"What does it cost?"** "About 1.4 ms per request sequentially, about 4 ms at p50 under 50 concurrent clients on a 4 vCPU VM, plus one judge call per evaluated reply, which the dashboard prices separately."
* **"What happens if the proxy dies?"** "Traces already in the spool survive a restart and are sent later. A power loss can lose the last few, and losing the volume loses what was in it."
