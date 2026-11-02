# Architecture and tradeoffs

The original gateway, Postgres evaluation queue and Streamlit interface are retained. The audit found repairable defects; replacing the application or adding Kubernetes would not address them.

## Request path

Authentication and process-local admission happen before reading a bounded body or contacting the configured provider. Secure mode limits enabled endpoint/method pairs. The proxy strips hop-by-hop and client credential headers, creates a trace event ID, forwards with a separate provider credential, and preserves ordinary completion/stream behavior. Proxy-only evaluation metadata is removed before forwarding.

An agent label is operational metadata, not customer identity. This release supports one organization per isolated deployment. A tenant column without authorization at every database and queue boundary would not create safe multi-tenancy.

## Trace acceptance and durability

Metadata minimization or explicitly enabled redaction happens before a trace reaches disk, broker or database. A non-streamed response acknowledges trace delivery only after queue acceptance. Database availability is decoupled from acceptance. Publication failure is explicit even if the provider already executed the request.

SQLite WAL with full synchronization is a bounded local durable spool on retained storage. It supports separate processes on one local filesystem; it is not a distributed queue or suitable for network filesystems. Pending and dead-letter payloads count toward capacity. Consumers claim with leases, retry with backoff and remove payloads only after a database commit. Expired leases recover work. Stable event IDs and a unique database index make redelivery idempotent at the sink.

In cloud mode, SQS Standard provides replicated queue persistence, visibility leases and managed dead-letter redrive. Encryption, retention, backlog alarms and IAM are deployment requirements. Duplicates are expected, and depth observations are approximate. Admission based on those observations is a guardrail, not an exact distributed capacity guarantee.

Provider execution and queue publication cannot share one transaction. A crash before finalization, especially during streaming, can leave provider activity without a finalized trace. Stream headers therefore say delivery is deferred. Monitor rejections and incomplete outcomes. This is not exactly-once model execution or lossless capture under arbitrary proxy termination.

## Database and worker transactions

Ingestion uses a small connection pool and bounded connection/query timeouts. Each insert commits before acknowledgement. A broker acknowledgement failure causes replay rather than a second trace row.

Evaluation uses Postgres as its durable job store. A short transaction selects work with FOR UPDATE SKIP LOCKED, increments the attempt and commits a lease token/expiry. Deterministic evaluation and remote judging occur outside that transaction. Completion is fenced by the token and saved atomically with versioned history. An expired final attempt becomes terminal instead of resetting its retry count after every crash.

This preserves the existing evaluation queue and avoids synchronizing two sources of job truth. A separate SQS evaluation queue is unnecessary at this scale; it could become useful if indexed queue scans constrain measured capacity.

Schema changes run under an advisory lock and transaction. Legacy scores are preserved and exact model prices take priority over family fallbacks. A version ledger records migrations. Production rollout still needs coordinated migrations and backup; transactional DDL alone does not prove backward compatibility.

## Evaluation trust and meaning

The relevance judge returns an opinion about whether a reply addresses the request. Strict parsing rejects malformed verdicts but does not make the judge truthful or injection-proof. History includes evaluator/configuration versions, sampling and call parameters.

Deterministic evaluators use recorded input and operator-owned references. A client selects a reference_id; it cannot declare its own answer trusted. Exact reference agreement, bounded schema validation and literal policy rules have narrow meanings. Lexical evidence coverage does not establish semantic groundedness. Missing evidence and unsupported checks produce skipped/insufficient-evidence outcomes.

## Monitoring and investigation

The dashboard authenticates before querying restricted views with parameterized filters. It distinguishes pending, processing, skipped, failed and scored relevance states; deterministic checks retain separate statuses/history. Unknown token counts/prices remain visible rather than appearing definitively free.

The asynchronous monitor compares disjoint windows with matching agent, model, operator request cohort and evaluator/config versions. It requires sufficient samples, an absolute decline and a conservative one-sided bound. The result is candidate score deterioration to investigate. Request mix, judge changes and correlated conversation turns remain alternative explanations. This is not causal model-drift detection. Acknowledgement requires an explicit capability/credential separate from the read-only dashboard role.

## Deployment boundary

Compose is local synthetic development. The AWS proposal uses ECS Fargate, RDS, SQS, managed secrets, private networking, HTTPS and CloudWatch. Google Cloud, Azure and simpler container platforms are also possible. These files do not establish a provider account, deployment, production capacity, disaster-recovery outcome or regulated compliance.
