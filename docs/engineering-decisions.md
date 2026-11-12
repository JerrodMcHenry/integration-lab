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

## ED-013 — PostgreSQL advisory locks, transaction-scoped, for per-account serialisation
**Decision:** `pg_advisory_xact_lock` on a dedicated connection, held for the
whole sync, keyed by a 64-bit hash of `(tenant_id, crm_account_id)`.
**Alternatives:** `SELECT … FOR UPDATE` on a lock row (needs a row per account
and an upsert to create it); session-scoped `pg_advisory_lock` (an explicit
unlock can fail and leave a pooled connection holding the lock); Redis locks
(another dependency, plus lease-expiry semantics); optimistic concurrency at
the destination (the mock CMS has no version field).
**Why:** No extra tables or services. Released automatically on commit,
rollback or session death.
**Tradeoffs:** Holds one connection and an open transaction per in-flight sync,
including during HTTP calls (bounded by client timeouts and
`idle_in_transaction_session_timeout`). Hash collisions are possible but only
cause unnecessary serialisation. Advisory locks don't work through a
transaction-pooling PgBouncer; session pooling or direct connections are required.

## ED-014 — Fail closed when the lock can't be acquired
**Decision:** If the database is unreachable or the lock times out, the sync
does not run. It is recorded as `failed` at stage `acquire_lock` with a
retryable error.
**Why:** Running unlocked could interleave writes from several workers. A
retryable failure is visible and safe; a silent race is neither.

## ED-015 — History is written inside the lock; persistence failures are surfaced
**Decision:** The history insert happens before the lock is released. If it
fails, `HistoryPersistenceError` carries the record, the record is logged at
ERROR (identifiers only), and the API returns 503 with
`X-Sync-History-Persisted: false` and the real outcome in the body.
**Why:** History order then matches execution order per account. A failed
history write must not hide the fact that the destination was changed.

## ED-016 — VARCHAR + CHECK instead of PostgreSQL ENUM types; `seq` identity for ordering
**Decision:** Status and stage are `varchar` with CHECK constraints. Ordering
uses a `GENERATED ALWAYS AS IDENTITY` column instead of timestamps.
**Why:** Adding a status later is a constraint swap in a migration, with no
`ALTER TYPE` limitations. Identity ordering is unambiguous even when several
records share a timestamp, and it matches the in-memory repository's
insertion order.

## ED-017 — Migrations are hand-written, forward-only by default
**Decision:** `0001` is written by hand (reviewable SQL). Its downgrade raises
unless `ALLOW_DESTRUCTIVE_DOWNGRADE=1`. `env.py` takes an advisory lock so
concurrent `alembic upgrade` runs (several containers) serialise. A test
compares the ORM metadata with the migrated schema to catch drift.
**Why:** Prevents accidental data loss and keeps models and migrations in sync.

## ED-018 — Integration tests use a dedicated `_test` database
**Decision:** Integration tests use only `TEST_DATABASE_URL`, abort unless the
database name ends in `_test`, create it if missing, migrate forward, and
`TRUNCATE sync_records` between tests. They skip when PostgreSQL is
unreachable, unless `REQUIRE_POSTGRES=1`.
**Why:** Real-database coverage without any chance of touching development data.
