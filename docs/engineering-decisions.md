# Engineering Decisions

Lightweight ADR log. Newest last.

## ED-001 — Synchronous `httpx` client for the CRM
**Decision:** `CRMClient` is synchronous.
**Why:** The client will mainly be called from background workers (M6), where
plain blocking code is simpler to reason about and test. FastAPI runs sync
routes in a threadpool, so request handlers are not blocked.
**Tradeoff:** Lower per-process concurrency than an async client. This can be
revisited if fan-out to the CRM becomes a bottleneck.

## ED-002 — Typed error hierarchy with a `retryable` flag
**Decision:** Every failure is converted to a `CRMError` subclass. Raw `httpx`
exceptions never leak to callers.
**Why:** The sync service and job queue need to tell "try again later" apart
from "this will never succeed" without inspecting HTTP details. This also
prevents silent failures: every outcome is either a valid payload or an
exception.

## ED-003 — Two-tier retry strategy
**Decision:** The client does a few fast retries (≤5, jittered, capped backoff)
for idempotent reads only. Long-horizon retries and dead-lettering belong to
the durable job queue (M6–7).
**Why:** Short blips shouldn't create job churn. Long outages shouldn't tie up
a worker thread in sleep loops. Writes are never auto-retried in the client,
because without idempotency keys that could duplicate side effects.

## ED-004 — No new dependencies for M2
**Decision:** Tests use `httpx.MockTransport` and Starlette's `TestClient`
(itself an `httpx.Client`) instead of `respx`/`responses`. The mock CRM token
endpoint parses form bodies with `urllib.parse` instead of adding
`python-multipart`.
**Why:** Keeps the dependency surface minimal. Injecting `TestClient` as the
CRM client's transport also gives true contract tests against the mock CRM
without needing network sockets.

## ED-005 — Fault injection lives in the mock CRM
**Decision:** `PUT /_admin/faults` makes the next N account reads fail with a
5xx or hang for a set delay.
**Why:** Demo step 4 (simulated failure) and the retry tests need
deterministic failures that go through the real HTTP path. The admin endpoints
are unauthenticated because the mock is a local dev tool, not a security
boundary. It must never be deployed as a real service.

## ED-006 — Field ownership belongs to the destination contract, not tenant config
**Decision:** `FIELD_OWNERSHIP` lives in `app/schemas/internal_customer.py`.
Tenant mapping files can choose *how* CRM data fills CRM-owned fields, but
cannot target internal- or system-owned fields.
**Why:** Ownership is a business rule between two systems of record. Letting a
mapping file redefine it would mean a single bad config could overwrite risk
classifications. Enforcement happens at config load, in the payload model, and
again right before each write (defence in depth), because the mock destination,
like many real internal APIs, does not protect those fields itself.

## ED-007 — Declarative JSON mappings with a whitelisted transform registry
**Decision:** Mappings are JSON (`app/mapping/configs/<tenant>.json`) validated
by Pydantic with `extra="forbid"`. Transforms are referenced by name from a
fixed registry, and each has a typed argument model.
**Why:** Customer-specific behaviour without arbitrary code execution: no
`eval`, no imports, no expression language, no YAML tags. Source paths are
regex-restricted, tenant ids are restricted (which also blocks path traversal),
and file size is capped.
**Tradeoff:** New transform types need a code change and review, which is the
intended control. Configs move to the database alongside tenants in M4/M5.

## ED-008 — Diff-then-patch with content-derived idempotency keys
**Decision:** Sync reads the destination record, compares CRM-owned fields
(type-normalised, e.g. `Decimal("9000000") == 9000000.0`), and PATCHes only
changed fields. Creates use `Idempotency-Key = tenant:external_id:sha256(body)`.
**Why:** Repeated syncs of an unchanged account produce no writes (tested), so
webhook redeliveries in M6 are harmless. Patching only changed fields keeps
the blast radius small and means destination-owned fields are never touched.
If a create's response is lost and the request is retried, the destination
returns the original customer instead of creating a duplicate (tested).
**Tradeoff:** One extra read per sync. There is a read-modify-write window
between our GET and PATCH. Within one process, per-account striped locks
serialise syncs of the same account. Across processes this needs a
DB-level lock or optimistic concurrency (M4).

## ED-009 — Stale-source protection via `LastModifiedDate`
**Decision:** If the CRM record is older than the `crm_last_modified_at` already
stored in the destination, the sync is recorded as `skipped_stale`.
**Why:** Webhooks and retries can arrive out of order. Without this check a
delayed retry could roll a newer record back.

## ED-010 — Sync history behind a repository Protocol
**Decision:** `AccountSyncService` depends on `SyncHistoryRepository`
(a `typing.Protocol`). Every read method requires `tenant_id`. The current
implementation is a bounded in-memory deque.
**Why:** PostgreSQL drops in during M4 without changing the service. Making
`tenant_id` a required argument makes cross-tenant reads a type error rather
than a forgotten `WHERE` clause.

## ED-011 — Expected failures are results, not exceptions
**Decision:** `sync_account` never raises for integration, mapping or
unexpected errors. It always returns, and persists, a `SyncRecord`. Unexpected
exceptions are logged with a stack trace and recorded as
`stage=internal, code=unexpected_error`.
**Why:** No silent failures. Every attempt is visible in history (and later in
the dashboard). The HTTP layer maps results to 201/200/404/422/502/504 and
always returns the record body.

## ED-012 — Fail-closed guard for mock admin endpoints; production config checks
**Decision:** The mock services mount `/_admin/*` only in development or test
(or when explicitly opted in for an unrecognised env). In
production/prod/staging the routes are never registered, and explicitly
enabling them raises at startup. IntegrationLab refuses to start in production
if dev-default secrets or non-HTTPS upstream URLs are configured.
**Why:** Fault injection and data seeding endpoints are dangerous if exposed.
Failing at startup makes the misconfiguration obvious instead of silently
ignoring it.
