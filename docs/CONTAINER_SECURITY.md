# Container security release gate

**Release remains blocked by HIGH/CRITICAL container findings.** Functional tests, dependency audits and non-root execution do not override this gate. No vulnerability exceptions, `--ignore-unfixed` settings, TLS bypasses or hidden scanner failures were used.

## Verified final-image checks

On 2026-11-02, all nine local image targets were scanned with Trivy `0.67.2`, pinned to `sha256:ac2f9d0197456a8ce460884b113e49d65b667f506c31d014c9955869a7a5d682`. The vulnerability database was updated on November 1 at 13:02 UTC and was still within its advertised freshness window (next update November 2 at 13:02 UTC). Exact image IDs were captured before and after each scan and remained unchanged. Raw output is under `artifacts/security/final-runtime-scan/`.

| Final target | HIGH findings | CRITICAL findings | Unique advisories | Fixed-version entries listed |
|---|---:|---:|---:|---:|
| Proxy, ingestion, evaluator, dashboard, monitor, demo and mock images (each) | 46 | 0 | 10 | 0 |
| Packaged client-only migration image | 56 | 0 | 11 | 0 |
| Upstream local `postgres:16` server image | 88 | 2 | 45 | 26 |

These are package/advisory findings, not independent exploits: one advisory may appear against several packages or images. A scanner identification plus a present component is verified evidence; application exploitability was not demonstrated. An empty fixed-version field means this scan did not list a fixed distribution package, not proof that no upstream remedy exists anywhere.

The committed [vulnerability inventory](container-vulnerability-inventory.json) contains every remaining advisory ID, severity, affected package/version, listed fix and image association. Generated scan reports are intentionally ignored rather than committing potentially sensitive future scan output.

## Remediations already verified

1. Application dependency installation remains signature/TLS verified, but `pip`, `setuptools` and `wheel` are removed from the final Python runtime filesystems. They are unnecessary for the immutable application. All seven images successfully imported their application dependencies as UID 10001, with all three tools absent. This removed six build-tool/vendor advisories per image, including critical `GHSA-6q48-6c7w-9gw2`; it did not suppress detection or delete evidence.
2. The migration image runs only `psql` as UID 999. Its unused `/usr/local/bin/gosu` executable was removed, eliminating 26 bundled-Go advisories from that artifact. Supported offline package minimization then marked `postgresql-client-16` manual and purged the unused `postgresql-16` server with automatic dependency removal. The resolved purge removed only seven server-side packages, including `libxml2`; no required client libraries or package evidence were deleted manually. This eliminated eight more advisories, including the remaining critical finding, from the migration artifact. Both `psql` and `pg_dump` 16.15 still run, packaged SQL is readable, and the server, `libxml2` and `gosu` are absent. Compose now builds and exercises this same client-only migration image rather than bind-mounting source SQL into the server image.
3. The upstream local Postgres server image was not modified or replaced to evade findings. The proposed production database is managed RDS; that choice is not evidence that a real RDS runtime has been tested or patched.

Evidence: `artifacts/security/runtime-tool-absence-imports.json`, `migration-client-only-feasibility.log`, `migration-client-dependencies.log`, `migration-client-only-check.json`, final scans and the before-remediation archives. The full isolated Docker end-to-end workflow passed after these final runtime packaging changes, including the client-only migration image: 11 traces, 1 intentional provider error, 3 streams, 9 scored, 2 skipped, 0 failed and 0 pending, with runtime-role ACL checks.

## Remaining actionable blockers

| Severity | Component and installed version | Verified location / scan evidence | Required action |
|---|---|---|---|
| Critical | `libxml2` `2.12.7+dfsg+really2.9.14-2.1+deb13u3`, `CVE-2026-6653` | Only the upstream local Postgres server image; actual library `/usr/lib/x86_64-linux-gnu/libxml2.so.2.9.14`; removed from the client-only migration artifact; no fixed distro version listed | Check authoritative distro/upstream security status, select a maintained patched server package/image when available and rescan. The current CI scans the local server and keeps this finding visible. |
| Critical | Go standard library `v1.24.6`, `CVE-2025-68121` | Only the upstream local Postgres server's `/usr/local/bin/gosu`; critical fix versions listed as `1.24.13`, `1.25.7`, `1.26.0-rc.3` | Use a maintained official server image/helper built with supported patched Go, checking all additional Go advisories rather than only this one. No arbitrary replacement binary was downloaded. |
| High | `libsqlite3-0` `3.46.1-7+deb13u2`, `CVE-2026-119373`, `CVE-2026-119586` | Python, migration and local server package inventories; no fixed distro version listed | Verify fixed distro packages/base images and assess actual enabled/reachable features. Keep the scan blocking until resolved through the reviewed release policy. |
| High | `ncurses`, `util-linux`, `libacl`, `libsystemd`/`libudev`, Perl package families | Ten shared OS advisories produce 46 findings in each Python image; exact versions/IDs in inventory | Track maintained distribution updates and rescan immutable images. Do not infer exploitability solely from a package name, and do not delete required libraries or hide the package database. |
| High | Additional migration/local Postgres GnuPG and Perl findings; only the local server retains `libxml2`/Go findings | Full component-specific inventory; migration's remaining findings have no listed fix | Review each relevant advisory and package status; remediate the maintained base or keep the artifact out of release. RDS deployment still requires independent runtime and recovery validation. |

Existing non-root users, read-only filesystems, dropped capabilities, private networking and bounded inputs reduce exposure, but they are not automatic exemptions for known vulnerabilities. No signed risk acceptance or production release approval is implied.

## Reproduce and advance the gate

Build the reviewed immutable images, run the CI container-security job or the same pinned Trivy command with `--exit-code 1 --severity HIGH,CRITICAL`, preserve reports and exact image digests, and verify the application end to end. Managed development environments must mount their trusted CA bundle for scanner HTTPS access. Never disable TLS or change expected checksums to make a scan finish.

Before a production claim, either deliver patched artifacts that satisfy the current gate or complete an explicit, evidence-backed security review and approved release policy change. This repository has done neither for the remaining findings. Cloud account/platform choice, paid deployment approval and real cloud validation are separate outstanding gates.
