# Interview preparation

How each part works, why it is built that way, and the questions it is likely to draw. Every number here comes from a run in this repository; the README says how to reproduce it.

## The one minute version

AgentEval is an OpenAI-compatible proxy. An agent changes its base URL and everything else stays the same. The proxy relays each request and stream unchanged, writes a redacted trace to a local durable spool before it returns, and a drainer moves traces into Postgres exactly once. A worker then grades each reply for relevance and, when the request opts in, checks every claim in the reply against the tool results the agent had. Anything not clearly supported goes to a human review queue with an audit trail. All checks run after the reply has been returned, so this is monitoring and review, not a guardrail.

## Components

### Proxy (`proxy/main.py`, `proxy/streaming.py`)

* **What:** FastAPI app with one shared httpx client. Relays server sent events as they arrive and rebuilds the full reply, including tool calls split across chunks, so it can be stored.
* **Why it is shaped this way:** The caller must not notice the proxy. So nothing slow happens before the response returns: the trace write runs in a thread, and the only change to the request is asking for usage on streams, which is stripped back out if the caller did not ask for it.
* **Hard parts:** Time to first token is measured at the first content delta, not the first byte. A mid-stream upstream failure cannot change the HTTP status any more, so the proxy sends a final event with an `error` object, which the OpenAI SDKs raise on. A client disconnect still produces a trace.

### Trace store (`proxy/store.py`)

* **What:** SQLite in WAL mode on a Docker volume as a local spool, plus one drainer thread that copies batches into Postgres with `ON CONFLICT (trace_id) DO NOTHING`.
* **Why not a message broker:** One container, no new infrastructure, and it removes the actual failure in the original code, which lost queued traces whenever Postgres was down or the proxy stopped. A broker becomes worth it with several proxy replicas or several consumers.
* **Guarantee:** A process crash or restart loses nothing that `submit()` accepted. Duplicates are impossible in Postgres because of the unique trace id. A power loss can lose the last moments because of `synchronous=NORMAL`.
* **Bounded:** At 50,000 spooled rows new traces are dropped and counted, and readiness returns 503, so an orchestrator can see it. The request itself never fails because of tracing.

### Eval worker (`eval/eval_engine.py`)

* **What:** Claims a row with a short `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`, commits, calls the judge with no transaction open, then writes the result only if its claim token still matches.
* **Why leases:** The original held a row lock across the judge call, so a slow judge pinned database connections and a dead worker held rows until its connection died. With a lease, a crashed worker's row comes back after the lease expires, and a worker that wakes up late cannot overwrite the result of the worker that took over.
* **Retries:** Attempts are counted at claim time with capped exponential backoff, so a row that crashes the worker every time still reaches the attempt limit and is marked failed with the reason.

### Grounded evaluation (`eval/grounded.py`)

* **What:** Two layers. A deterministic check normalizes identifiers (money, CARC and RARC codes, claim ids, dates, day counts, CPT codes) and flags any in the reply that are not in the evidence. A judge labels each claim supported, contradicted or unsupported with evidence ids. Code, not the model, combines them into one verdict.
* **Why two layers:** The deterministic layer is cheap, explainable and catches the most damaging errors in this domain: invented amounts, codes and dates. It cannot catch wrong reasoning or a superseded policy cited correctly. The judge covers those, at a cost and with its own failure modes.
* **Why code decides the verdict:** It keeps the rule auditable and stable across judge models, and it means a judge that says "looks fine" cannot override an invented dollar amount.

### Human review (`dashboard/reviews.py`)

* **What:** A state machine (pending, approved or rejected, resolved) with a version check in the same `UPDATE`, a unique review per evaluation, and an append-only event table.
* **Why:** Two reviewers clicking at once must not both win, a retried worker must not open two reviews, and a decision must not be rewritten afterwards.

### Dashboard (`dashboard/app.py`, `sql/init.sql`)

* **What:** Streamlit over SQL views. The dashboard only formats numbers, and a test recomputes them independently.
* **Why cost is a view:** A price correction fixes history, and an unknown model is shown as unpriced instead of being priced at the wrong rate.

## Likely questions

**Why a proxy instead of an SDK?** No code change in the agent beyond the base URL, it works for any language, and the agent cannot forget to log. The cost is an extra hop, about 1.4 ms sequentially and about 4 ms at p50 under 50 concurrent clients on a 4 vCPU VM.

**What happens when Postgres goes down?** Requests keep working, readiness says degraded, and traces wait in the spool. The outage drill stops the real container, restarts the proxy with 20 traces spooled, and checks all 25 traces land exactly once. The longest delay in that run was 21 seconds.

**How do you know the judge works?** I do not, yet, for a real model. The labeled dataset has 24 synthetic cases. The deterministic layer alone gets 8 of 21 unambiguous cases right with review recall of 0.529. The offline stub's numbers describe the stub, not a judge. Measuring a real judge is the first planned step, and the dataset needs a second, independent labeler.

**Why are the mock results reported at all?** To show the pipeline end to end offline and to make the gap visible. They are labeled as a stub everywhere they appear.

**Can a reply manipulate its own grade?** It can try. The reply and evidence are passed as JSON data with an instruction to ignore embedded instructions, the output is strictly parsed, and the final verdict is computed in code, so an injected "rate this supported" cannot override an invented identifier. Dataset case J2 is still missed by the deterministic layer. This is mitigation, not prevention.

**Why is review after the fact?** Grading takes a second judge call, and blocking every reply on it would add that latency to every request. Pre-response gating for selected high risk actions is planned, not built.

**Is this HIPAA compliant?** No. Redaction is pattern based and only applies to what is stored; the model provider still receives the original request. There is one shared dashboard password and no per user identity. `docs/SECURITY.md` lists what would be required.

**How would it scale?** The proxy is CPU bound in Python under heavy concurrency. Add uvicorn workers or replicas, each with its own spool. Workers already scale horizontally on the lease queue. At high volume, move the spool to a managed queue, partition traces by month, and sample evaluations.

**What would you change first with more time?** Run the dataset with a real judge and get independent labels, add per user auth for reviews, and record the Responses API.

**What broke while building it?** A legacy `evaluations` table from the oldest schema collided with the new evaluation table, so the new one is `trace_evaluations`. The original identifier matcher used substring search, so `$43` matched `$430.00`; it now compares normalized sets. Closing the test TCP relay's socket did not wake its accept loop until it called `shutdown` first.

**What are the known failure cases?** Power loss can lose the last few spooled traces. Losing the spool volume loses its contents. Several proxies cannot share one spool. A database user can delete audit events. The grounding judge is unmeasured.
