# Engineering report

Branch `cursor/rcm-eval-reliability-44ca`, compared with `main` at `d888c45`. All results below were produced on one Linux x86_64 VM (4 vCPU, Python 3.12.3, PostgreSQL 16, Docker 29.1) with no LLM API key.

## Verdict: READY WITH DISCLOSED LIMITATIONS

Ready to demo locally on synthetic data. The disclosed limitations:

1. **The grounding judge has not been measured with a real LLM.** No API key was available. The offline demo uses a keyword stub, whose numbers say nothing about real judge quality.
2. **The dataset is small and labeled by one person,** the same person who wrote the evaluator.
3. **Apple Silicon has only been tested under emulation.** The services were installed and run in an arm64 container under QEMU on x86. The full stack has never been built on a real Mac.
4. **Every check is retrospective.** Bad replies are flagged after they reach the caller, not blocked.
5. **Not for real patient or customer data.** Redaction is pattern based and applies to storage only. There is one shared dashboard password and no per-user identity. No compliance claim is made.

Passing these tests does not make the system production ready.

## Status by stage

| Stage | Status | Evidence |
|---|---|---|
| 1. Audit | Done | `docs/AUDIT.md`: baseline of 30 tests passing and e2e passing on the original code; six defects reproduced with probes before any change |
| 2. Defect fixes | Done | Each defect below has a regression test |
| 3. Durable ingestion | Done | `proxy/store.py`; 23 reliability tests; outage drill against the real Postgres container |
| 4. Grounded evaluation | Done, accuracy measured offline only | `eval/grounded.py`; 29 tests; 24-case labeled dataset; deterministic and mock reports in `eval/results/`; real judge blocked |
| 5. Human review | Done | `dashboard/reviews.py`; 9 tests; e2e runs the review CLI through pending, rejected and resolved, and confirms a duplicate decision is refused |
| 6. Security and privacy | Done for demo scope | `docs/SECURITY.md`; redaction, retention, auth gates, log check in e2e, pip-audit clean |
| 7. Observability | Done | `/health/live`, `/health/ready`, `/metrics`, JSON logs, worker heartbeats, dashboard alerts |
| 8. Denial demo | Done | `demos/rcm_denial/`, isolated from the core; `test_rcm_demo.py`; e2e checks the three verdicts |
| 9. Testing | Done | 103 tests, e2e, drill, dataset, audit (below) |
| 10. Setup and docs | Done; native arm64 untested | README, `docs/DESIGN.md`, `docs/CLOUD_ASSESSMENT.md` (proposal only) |
| 11. Demo and interview prep | Done | `docs/DEMO_SCRIPT.md` (outage step rehearsed on the live stack), `docs/INTERVIEW_PREP.md` |

## Defects fixed

| Defect from the audit | Fix | Regression test |
|---|---|---|
| Requested model stored, served model ignored | `response_model` column; cost uses `coalesce(response_model, model)` | `test_reliability.py` model attribution tests |
| Traces lost when Postgres down or proxy stopped | Durable SQLite spool, drainer, idempotent inserts | Outage, restart, idempotency, bound tests; drill |
| NUL character silently dropped the trace | Replaced before storage | NUL test |
| Mid-stream upstream failure recorded as success | Error event to caller, `error_type` on trace | Mid-stream failure test |
| Every transport failure was 502 | Timeouts 504, connection failures 502, classified | Timeout test |
| Non-chat streams buffered fully | Relayed incrementally | `/v1/responses` streaming test |
| Row lock held across judge call | Lease, claim token, short transactions | `test_no_transaction_is_open_during_the_judge_call`, `test_expired_lease_is_reclaimed_and_the_stale_worker_cannot_overwrite` |
| Malformed response crashed the worker in a loop | Per-row isolation; attempts counted at claim | Poison row test |
| Hidden SDK retries and uncounted judge tokens | `max_retries=0`; tokens from failed attempts stored | Judge accounting test |
| Prompt injection through tag closing | JSON-encoded judge input | Injection test; dataset J1, J2 |
| `/health` always ok, no readiness | Live and ready endpoints, spool-aware | Health tests |
| Dashboard: missing usage shown as $0, interrupted streams as success | `usage_missing`, `failed` and `completed` in views; independent recomputation test | `test_dashboard.py` |
| No request size limit | 413 above `MAX_REQUEST_BYTES` | Size limit test |

## Commands run and results

| Command | Result |
|---|---|
| `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest -q` | **103 passed**, 0 failed, 0 skipped (24.8 s) |
| `scripts/e2e_offline.sh` | Passed: 11 support traces with expected statuses, response_model recorded on 10, grounded verdicts supported / unsupported / insufficient_evidence, 2 pending reviews, 0 unredacted contact details in Postgres, audit trail pending>rejected>resolved, metrics written_total 17, logs clean |
| `scripts/outage_drill.sh` | Passed: 25 of 25 traces, 25 distinct ids, 20 recovered across a proxy restart during the outage, max ingestion delay 21 s |
| `python eval/run_dataset.py --judge deterministic` | 8 / 21 unambiguous correct, 3 / 3 ambiguous acceptable, review recall 0.529 |
| `python eval/run_dataset.py --judge mock ...` | 8 / 21, 2 / 3, recall 0.941 (stub, not a quality measure) |
| `python eval/run_dataset.py --judge openai` | **Not run**: no API key |
| `python bench/overhead.py` | +1.4 ms p50 sequential; +3.7 ms p50 at 50 concurrent clients; 441 vs 467 req/s; 3,050 of 3,050 traces recorded |
| `pip-audit -r <each requirements.txt>` | No known vulnerabilities in all six files |
| arm64 under QEMU: install each requirements file, import modules, run dataset, start proxy | All installed and imported; dataset results identical; `/health/live` and `/health/ready` answered |
| Original `main` benchmark on the same VM, for comparison | +1.1 ms p50 sequential; +1.5 to 1.8 ms p50 concurrent; about 454 to 458 req/s |

## Files changed

46 files, about 4,700 lines added and 330 removed, excluding the docs added in the final commit. No existing feature was removed. The support demo, relevance scores, statuses, key rules, SQL cost and both upgrade paths all still pass their original tests.

* **Core:** `proxy/main.py`, `proxy/store.py`, `proxy/streaming.py`, new `proxy/redaction.py`; `eval/eval_engine.py`, new `eval/grounded.py`; `dashboard/app.py`, new `dashboard/reviews.py` and `dashboard/review_cli.py`; `sql/init.sql`.
* **Evaluation:** `eval/datasets/grounded_rcm_v1.json`, `eval/run_dataset.py`, `eval/results/`.
* **Demo:** `demos/rcm_denial/`, `mock_upstream/server.py` (scripted replies, failure triggers, grounding stub).
* **Tooling:** `docker-compose*.yml`, Dockerfiles, `Makefile`, `.env.example`, `.github/workflows/ci.yml`, `scripts/e2e_offline.sh`, new `scripts/outage_drill.sh`, `bench/overhead.py`.
* **Tests:** new `test_reliability.py`, `test_grounded.py`, `test_reviews.py`, `test_rcm_demo.py`; extended `test_eval.py`, `test_dashboard.py`, `test_migrations.py`, `conftest.py`.
* **Docs:** `README.md`, `docs/AUDIT.md`, `docs/DESIGN.md`, `docs/SECURITY.md`, `docs/DEMO_SCRIPT.md`, `docs/INTERVIEW_PREP.md`, `docs/CLOUD_ASSESSMENT.md`, this report.

## Risks

* **Judge quality is unknown.** A real judge could be worse than the deterministic layer on some categories, for example by labeling a superseded policy as supported. Measure before relying on any verdict.
* **Throughput cost of durability.** About 2 ms more at p50 under 50 concurrent clients than the in-memory writer. Acceptable here; revisit if latency budgets are tight.
* **Power loss window.** `synchronous=NORMAL` can lose the last moments of spooled traces in an OS crash.
* **Retention removes audit events** together with their reviews.
* **Reviewer identity is self-declared.**
* **Environment notes:** two changes were made to this VM, not to the project: an iptables FORWARD policy so Docker bridge traffic works, and a binfmt_misc mount for arm64 emulation.

## Demo instructions

```bash
make offline && make offline-rcm-demo   # then open http://localhost:8501
```

The timed script and recording sequence are in `docs/DEMO_SCRIPT.md`.

## Next steps, in order

1. `make dataset JUDGE=openai` with a key, then review the false positives and false negatives by category.
2. Have a second person label the dataset independently, and grow it beyond 24 cases.
3. Build the full stack on an Apple Silicon Mac: `make offline && make e2e`.
4. Single sign-on and roles for review actions.
