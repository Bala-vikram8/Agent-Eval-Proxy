# Threat model and security boundaries

Scope: one organization per isolated deployment, synthetic data for development. This document is an engineering threat model, not a regulatory certification. The release is not approved for production or sensitive customer information until the deployment acceptance gates have been exercised.

## Assets and trust boundaries

- Provider credentials authorize paid model calls. Proxy credentials authorize access to this deployment and must never substitute for provider credentials.
- Messages, replies, tool arguments, evaluation evidence and reasons may contain confidential information. Queue payloads and dead letters are as sensitive as database rows.
- Caller-supplied agent labels, metadata, model output, tool arguments, judge replies and HTTP headers are untrusted input. An authenticated client does not become an administrator or a source of verified truth.
- Operator-controlled reference catalogs/configurations are privileged inputs. Only the deployment operator can designate trusted expected answers. A reference comparison proves agreement with that configured reference, not that the reference is true.
- The browser/dashboard, proxy, queue, ingestion worker, evaluator, monitor and database are separate boundaries. Each receives only the credentials it needs.

## Threats and required controls

| Threat | Control and verification | Remaining limitation |
|---|---|---|
| Unauthorized model use or trace browsing | Fail-closed application authentication; explicit insecure-local mode for synthetic demos; dashboard gate before SQL; negative auth tests | Shared application secrets do not replace cloud SSO, session policy, MFA or per-user audit trails. Internet-facing access is not approved. |
| Provider credential exfiltration | Separate proxy/upstream credentials, fixed configured upstream origin, no redirect following, reject URL credentials, strip hop-by-hop/client auth headers, safe error categories | A chosen upstream necessarily receives its credential. Operators must trust/allowlist that destination and restrict egress. |
| Confidential data stored in traces/queues | Metadata-only default; optional redaction before persistence; synthetic PII/token/confidential-field tests; bounded capture and protected spool files | Patterns cannot discover arbitrary personal names or business secrets in prose, images, encoded content or custom formats. Redacted mode is not anonymization. Prefer metadata-only until a domain-specific privacy review. |
| Prompt injection against judge | Judge input treated as data, strict verdict types, deterministic evaluators and regression inputs, operator reference catalog | Prompt wording cannot make an LLM judge injection-proof; relevance is an opinion. Injection evaluation must also be tested with the intended real judge. |
| Request/stream/resource exhaustion | Body, capture, event, concurrency/rate and queue bounds; overload errors; finite retries and DLQ; timeouts | Per-process limits are not a distributed quota. Add a shared edge limiter/WAF and load-test the deployment. |
| Trace loss or duplicate processing | Durable acceptance, stable event IDs, idempotent sink, delete/ack after database commit, leases, retries, recovery tests | Model execution and trace delivery are not one atomic transaction. A provider may have completed before a trace publish failure; retrying a request can incur a second model charge. Stream headers precede final trace acceptance. |
| Database credential abuse | Parameterized SQL, separate migration/ingestion/evaluation/read-only roles, private database ingress, encrypted connections in cloud | Local Compose credentials are deliberately synthetic. Deployment secrets must be managed independently and rotated. |
| Over-retention/deletion mismatch | Bounded administrative retention, dependent evaluation cleanup, queue/DLQ retention configuration, explicit deletion/runbook | Backups and queued events have separate lifetimes. Restore procedures must reapply deletion policy; end-to-end erasure is not proven by deleting one SQL row. |
| Build/dependency compromise | Pinned direct dependencies, executable vulnerability scans, container image inspection, no TLS/checksum bypass, CI read-only defaults | A clean scanner report is time-bound and not proof of absence of vulnerabilities. Review transitive changes and base-image updates. |
| Cross-customer disclosure | One organization per deployment; no customer tenancy feature | Do not share one deployment/database/dashboard among independent customers. Tenant IDs alone are not isolation. |

## Authentication and secrets operations

Use independently generated high-entropy proxy and dashboard credentials. Keep provider credentials server-side. Supply secrets through deployment-managed bindings or supported secret-file paths; never commit values or include them in an issue/report. Rotate the consumer credential and provider credential independently. During a suspected leak, revoke at the provider first, stop ingress, preserve metadata-only incident evidence, and issue replacement credentials through the secret manager.

The local offline mode must be explicitly enabled and bound to loopback. It must not be used to expose a public deployment. Cloud HTTPS should terminate at an authenticated boundary, with workloads/private database access restricted by security groups and TLS verification enabled for outbound calls. Default cloud design is internal access; a public endpoint is a separate reviewed change.

## Verification before a production claim

Run the synthetic security/failure tests, dependency and image scans, real-provider calibration, authenticated cloud smoke tests, load and capacity tests, restart/recovery tests, backup restore and deletion-policy exercises. Record exact versions, scope, results and unresolved risks. Define incident ownership, alerts, recovery objectives and budget limits. Neither local test success nor infrastructure files establish that a cloud environment was deployed or validated.
