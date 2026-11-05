# Cloud deployment assessment

**Proposal only.** Nothing has been deployed, no cloud account has been used, and no cost has been incurred. Any deployment needs explicit approval first. Cost figures are deliberately absent: estimate them with the provider's pricing calculator for the expected traffic before deciding.

## What changes when it leaves a laptop

| Concern | Local demo today | Needed in a cloud environment |
|---|---|---|
| Network | Ports bound to 127.0.0.1 | Private subnets only. Proxy reachable from the agents' network through an internal load balancer; dashboard behind an identity aware proxy or VPN. No public endpoints |
| Identity | Shared proxy key, shared dashboard password | Per service credentials for agents; single sign-on with roles for the dashboard and review actions |
| Secrets | `.env` file | Managed secret store, injected at runtime, rotated |
| Database | Postgres container, local volume | Managed Postgres with encryption at rest, automated backups, point in time recovery, a tested restore, and least privilege roles (the proxy drainer only inserts traces; the dashboard reads and writes reviews only) |
| Trace spool | SQLite on a Docker volume | Per replica persistent volume, or replace with a managed queue once there are several replicas. The spool's guarantees in `docs/DESIGN.md` assume one writer per file |
| Workers | One container | Several replicas; the lease queue already supports it. Alert on heartbeat age and backlog age |
| TLS | None | TLS on every hop, including to Postgres |
| Observability | `/metrics`, JSON logs, dashboard alerts | Prometheus or a managed equivalent scraping `/metrics`, log aggregation with content excluded, paging on readiness failures, spool depth, dead letters and worker staleness |
| Data lifecycle | Opt-in `RETENTION_DAYS` | Retention agreed with data owners, monthly partitions for `traces`, audit export to append-only storage |
| Delivery | `docker compose` | Images built and scanned in CI, pushed to a private registry, deployed by pipeline with migrations as a separate step |

## Suggested shape

A container service (for example Kubernetes, or a managed container platform) running:

* **proxy**: 2 or more replicas, each with its own small persistent volume for the spool, readiness probe on `/health/ready`, liveness on `/health/live`, graceful shutdown period of at least 15 seconds.
* **eval worker**: 1 or more replicas, scaled on backlog age.
* **dashboard**: 1 replica behind single sign-on.
* **migrate**: a one-off job before each rollout.
* **Postgres**: managed, private, with backups.

## Risks to resolve before a pilot

1. The grounding judge has not been measured with a real model. Do this before anyone relies on a verdict.
2. Model provider data handling: confirm what each provider stores and for how long, and whether a BAA is available if regulated data is ever in scope.
3. Judge cost grows with traffic. Add sampling before enabling grounding on every request.
4. Spool behavior with multiple replicas and rolling restarts needs its own drill on the target platform.
5. The review workflow trusts the typed reviewer name until single sign-on is in place.

## Not recommended

* Exposing the proxy publicly. It injects a paid API key, so a public proxy is an open relay billed to the account owner.
* Running the dashboard without authentication on any network shared with other people.
* Using real patient or customer data before the items in `docs/SECURITY.md` are done.
