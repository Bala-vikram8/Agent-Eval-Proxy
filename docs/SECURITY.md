# Security and privacy

What this repository does to protect data, what it does not do, and what would have to happen before it handled regulated data such as protected health information.

**This project is not HIPAA compliant and holds no security certification.** No risk assessment, penetration test or compliance review has been carried out. Use synthetic data only.

## Threat model for the demo

The intended deployment is one engineer's laptop or a private development VM, with every port bound to 127.0.0.1 and only synthetic data. The controls below are sized for that. They are not sized for a shared network, several tenants or real patient data.

## Implemented controls

| Area | Control | Where | Verified by |
|---|---|---|---|
| Network exposure | Every published port (proxy, dashboard, Postgres) binds to 127.0.0.1 | `docker-compose.yml` | Inspection |
| Proxy access | Optional `PROXY_API_KEY`, compared in constant time. The proxy replaces it with the upstream key | `proxy/main.py` | `test_proxy.py` |
| Key isolation | `OPENAI_API_KEY` is only sent to api.openai.com, for both the proxy and the judge | `proxy/main.py`, `eval/eval_engine.py` | `test_proxy.py` |
| Metrics | `/metrics` requires the proxy key when one is set | `proxy/main.py` | `test_reliability.py` |
| Dashboard access | Optional `DASHBOARD_PASSWORD`, compared in constant time. The dashboard warns when none is set | `dashboard/app.py` | `test_dashboard.py` |
| Proxy-only headers | `X-AgentEval-*` headers are validated and never forwarded upstream | `proxy/main.py` | `test_reliability.py` |
| Storage minimization | SSNs, emails, US phone numbers and Luhn-valid card numbers are replaced with `[REDACTED_*]` before a trace reaches the spool or Postgres. A per-trace count is stored | `proxy/redaction.py` | Unit tests; the e2e script checks no synthetic contact detail reaches Postgres |
| Logs | JSON logs carry ids, statuses and timings, never message content or credentials | `proxy/main.py`, `eval/eval_engine.py` | The e2e script searches every service log for contact details and credentials |
| Retention | Opt-in `RETENTION_DAYS` purges traces with their evaluations and reviews | `sql/init.sql` | `test_eval.py` |
| Input limits | Request body limit (`MAX_REQUEST_BYTES`), upstream timeout | `proxy/main.py` | `test_reliability.py` |
| Judge prompt injection | Reply and evidence are passed as JSON data with an instruction to ignore embedded instructions; judge output is strictly parsed; final verdicts are computed in code | `eval/grounded.py`, `eval/eval_engine.py` | Dataset cases J1 and J2 (J2 is missed by the deterministic layer) |
| Audit | Review decisions are written to `review_events`, which rejects updates | `sql/init.sql` | `test_reviews.py` |
| Dependencies | Pinned versions; `pip-audit` in CI | `.github/workflows/ci.yml` | Clean at the time of writing |

## Important gaps

* **Redaction applies to storage, not to the model provider.** The proxy forwards the request unchanged, so whatever the agent sends reaches the upstream model provider, including any personal data. The judge receives the redacted copy from Postgres.
* **Redaction is pattern matching, not de-identification.** Names, addresses, dates of birth, member ids, free text clinical details and identifiers in unusual formats are not detected. Claim numbers, codes, amounts and dates are kept on purpose because the grounding check needs them.
* **No user identity.** The dashboard has one shared password. The reviewer name on a decision is typed in and not verified. There are no roles, so anyone who can open the dashboard can approve or reject.
* **No TLS.** Traffic between services and to the browser is plain HTTP on localhost.
* **Encryption at rest** is whatever the host disk provides. Postgres, the spool volume and backups are not encrypted by this project.
* **Default database credentials** in `.env.example` are only acceptable because the port binds to localhost.
* **Audit trail limits.** Anyone with direct database access can delete review events or drop the trigger. Retention deletes events together with their reviews.
* **Prompt injection** is mitigated, not prevented. A reply can still try to steer the judge.
* **Streamlit** serves the dashboard without CSRF protection beyond Streamlit's defaults and has not been hardened.

## Before handling regulated data

All of the following would be needed, and none of it is done:

1. A risk assessment and a written data flow for every place data goes: agent, proxy, model provider, judge provider, spool volume, Postgres, backups, logs, browser.
2. A business associate agreement with each model provider that would receive PHI, or a self-hosted model, and configuration that guarantees requests only go to those providers.
3. De-identification before data leaves the agent or the proxy, with a measured miss rate on representative synthetic data, or a decision that the stored copy is in scope and protected as PHI.
4. Single sign-on with per-user identity, roles (viewer, reviewer, admin) and authorization checks on every review action, with the authenticated identity written to the audit trail.
5. TLS everywhere, private networking, no public endpoints, and secrets in a managed secret store.
6. Encryption at rest for the database, the spool volume and backups, with key management.
7. An audit log stored outside the application's write path (for example append-only object storage), access logging for every read of trace content, and a retention policy that satisfies both privacy and audit requirements.
8. Tenant isolation if more than one customer's data is involved.
9. Vulnerability management, penetration testing, an incident response procedure and breach notification process.
10. Independent compliance review. Passing local tests is not evidence of compliance.

## Reporting a problem

Open a private security advisory on the GitHub repository rather than a public issue.
