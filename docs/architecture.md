# Architecture

> Status legend: ✅ implemented and tested · ⚠️ implemented with a documented limitation · 🚧 planned (not built)

## Context

IntegrationLab synchronizes customer account records for **Meridian Capital**
from a Salesforce-style CRM into Meridian's internal Customer Management
System (CMS). Both external systems are simulated by separate mock services.

## Current state (Milestone 7 complete)

```
                     ┌──────────────────────────── IntegrationLab (FastAPI :8000) ───────────────────────────┐
 POST /sync/accounts │  api/sync.py ──▶ services/account_sync.py (orchestration only)                         │
 GET  /sync/history  │                   │            │                 │                     │               │
                     │                   ▼            ▼                 ▼                     ▼               │
                     │   integrations/crm_client  mapping/mapper  integrations/internal_client  repositories/  │
                     │   (OAuth2 CC, retries)     (JSON config,   (API key, idempotent         sync_history   │
                     │                            whitelisted     creates, retries)            (Protocol +    │
                     │                            transforms)                                   PostgreSQL)   │
                     └───────────│──────────────────────────────────────────│────────────────────────────────┘
                                 ▼                                           ▼
                     Mock CRM (:8001)                              Mock Meridian CMS (:8002)
                     OAuth token endpoint, expiring tokens        customers keyed by external_crm_id
                     GET/PATCH accounts                           POST (Idempotency-Key, 409 on dup), PATCH
                     /_admin faults (dev only)                    /_admin faults (dev only)
```

| Component | Path | Status |
|---|---|---|
| Health and readiness endpoints | `app/main.py` | ✅ |
| Environment config, production safety checks | `app/core/config.py` | ✅ |
| CRM client | `app/integrations/crm_client.py` | ✅ |
| Internal CMS client | `app/integrations/internal_client.py` | ✅ |
| Shared error base + bounded retry | `app/integrations/errors.py`, `retry.py` | ✅ |
| Destination contract & field ownership | `app/schemas/internal_customer.py` | ✅ |
| Customer-specific field mapping | `app/mapping/` | ✅ file-based |
| Sync service | `app/services/account_sync.py` | ✅ |
| Sync history | `app/repositories/sync_history.py` | ✅ PostgreSQL (in-memory fake for unit tests only) |
| Per-account concurrency lock | `app/services/locks.py` | ✅ PostgreSQL advisory locks (in-process fake for unit tests only) |
| Schema & migrations | `app/db/models.py`, `migrations/` | ✅ Alembic `0001` |
| Docker Compose stack | `docker-compose.yml`, `Dockerfile` | ✅ |
| Sync API | `app/api/sync.py` | ✅ OIDC-authenticated, RBAC, tenant from token |
| Authentication & RBAC | `app/auth/` | ✅ JWT (RFC 9068) verification, permissions |
| Tenants, per-tenant integrations, versioned mappings | `app/repositories/tenants.py`, migration `0002` | ✅ |
| Per-tenant clients and secret references | `app/services/tenant_context.py`, `secrets.py` | ✅ |
| Admin API (own tenant only) | `app/api/admin.py` | ✅ |
| Mock IdP | `mock_idp/` | ✅ dev/test only |
| Webhook ingestion (signed, replay-protected, persisted before ack) | `app/webhooks/`, `app/api/webhooks.py` | ✅ |
| Durable event inbox | `app/repositories/webhook_events.py`, migration `0004` | ✅ |
| Background worker (SKIP LOCKED claiming, leases) | `app/worker/` | ✅ |
| Mock CRM / mock CMS / admin guard | `mock_crm/`, `mock_internal/`, `mock_common/` | ✅ |
| Automatic retries (bounded exponential backoff + jitter), dead-letter, manual replay | `app/worker/retry_policy.py`, `app/api/webhooks.py` | ✅ |
| Attempt history, audit log, worker heartbeats | migration `0005` | ✅ |
| Lock-ownership verification + processing deadline before destination writes | `app/services/account_sync.py` (`_WriteGate`) | ✅ (limitations below) |
| Operational metrics (tenant JSON + platform Prometheus) | `app/api/ops.py` | ✅ |
| Retention policy (dry-run by default) | `app/retention.py`, `python -m app.cli retention` | ✅ |
| React monitoring dashboard | — | 🚧 M8 |

## Separation of responsibilities

| Module | Knows about | Does not know about |
|---|---|---|
| `crm_client` | CRM HTTP API, OAuth | mapping, destination, history |
| `internal_client` | CMS HTTP API, idempotency keys | CRM, mapping, ownership rules |
| `mapping` | CRM field names → destination fields, transforms | HTTP, storage |
| `schemas/internal_customer` | destination fields and **who owns each one** | everything else |
| `account_sync` | the order of steps, diffing, ownership enforcement, result recording | HTTP status codes, storage engine |
| `repositories/sync_history` | storing and querying `SyncRecord`s | how they were produced |
| `services/locks` | mutual exclusion per (tenant, account) | what runs inside the lock |

## Synchronization flow

`AccountSyncService.sync_account(tenant_id, crm_account_id)`:

1. **Resolve mapping** for the tenant. An invalid or missing config fails
   immediately, before any network I/O.
2. **Extract**: `CRMClient.get_account`.
3. **Transform**: `FieldMapper.map` → `CustomerSyncPayload` (CRM-owned
   fields only, `extra="forbid"`). All data issues are reported together.
4. **Load**:
   - look up the destination customer by `external_crm_id`;
   - not found → `POST` create with a content-derived `Idempotency-Key`. On
     `409` (another writer won the race), re-read and continue as an update;
   - found and the source's `LastModifiedDate` is older than the destination's
     `crm_last_modified_at` → `skipped_stale` (protects against out-of-order
     events, which matters for webhooks in M6);
   - otherwise diff the CRM-owned fields only → `PATCH` just the changed fields,
     or `unchanged` with no write.
5. **Record**: exactly one `SyncRecord` per call, success or failure, added to
   history **while the lock is still held**, then returned.

Steps 1–5 all run inside the per-(tenant, account) lock (see below). If the
lock can't be acquired, the sync is recorded as `failed` at stage
`acquire_lock` and **no upstream call is made** (fail closed).

### Result model

`status ∈ {created, updated, unchanged, skipped_stale, failed}`. Failures carry
`stage ∈ {acquire_lock, fetch_source, transform, load_destination, internal}` and an `error`
with a stable `code`, a `retryable` flag, the upstream HTTP status, and (for
mapping failures) per-field `issues`. Records contain identifiers and field
**names** only, never account values.

### Field ownership

Ownership is defined once in `FIELD_OWNERSHIP` and enforced in three layers:

1. **Config load:** a mapping that targets an `internal`- or `system`-owned
   field is rejected.
2. **Payload model:** `CustomerSyncPayload` has only CRM-owned fields and forbids extras.
3. **Before every write:** `_assert_crm_owned_only` raises if any other field is present.

The mock CMS deliberately does **not** protect `onboarding_status` or
`risk_classification` (its API is shared with Meridian staff tools), so
IntegrationLab's enforcement is what actually protects them. This is tested end to end.

CRM-owned values that someone edited by hand in the destination are put back
to the CRM value on the next sync, because the CRM is the system of record for
those fields. An optional CRM field that is empty clears the destination value.

## Retry model (two tiers)

- **In-client (implemented):** at most `*_MAX_RETRIES` (0–5, enforced in
  config *and* in `call_with_retry`), exponential backoff with full jitter and
  a cap. Only `retryable` errors are retried. CRM: GET only. CMS: GET/PATCH,
  plus POST because creates carry an `Idempotency-Key`.
- **Job-level (🚧 M6–7):** a durable queue re-runs failed syncs where
  `error.retryable` is true, with bounded attempts and a dead-letter queue.
  Right now a failed sync is recorded and reported but **not** retried
  automatically. The caller (or, later, the worker) has to try again.

## Persistence (Milestone 4)

```
 uvicorn worker 1 ─┐                                  ┌─ sync_records (history)
 uvicorn worker 2 ─┼─ SQLAlchemy pool (per process) ──┤
 uvicorn worker N ─┘                                  └─ pg_advisory_xact_lock(key)   (no table)
                                   PostgreSQL 18 (docker compose, 127.0.0.1:5434)
```

- **Wiring:** `app/wiring.py` always builds the PostgreSQL implementations.
  `DATABASE_URL` is required in every environment: the API, worker and CLI
  refuse to start without it. In-memory fakes live only in `tests/support/fakes.py`.
- **Engine:** `pool_pre_ping` (survives DB restarts), `pool_recycle=1800`,
  bounded pool (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`),
  connect timeout, `hide_parameters=True` so SQL parameters never reach logs.
- **Transactions:** each history write is one `session.begin()` block that
  COMMITs on success and ROLLs BACK on any exception. Reads use a short session.
  A sync uses **two** connections: one holds the lock transaction (no writes),
  the other writes history. Pool sizing must allow 2 × concurrent syncs per process.

### Table `sync_records`

Stores identifiers, outcome, timing, mapping version and error metadata. It
has **no account payload columns** (verified by a test listing every column).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | generated by the app |
| `seq` | bigint identity (always), unique | insertion order. "Most recent" = `seq DESC` |
| `tenant_id` | varchar(63) | CHECK `^[a-z0-9][a-z0-9-]{0,62}$` |
| `crm_account_id` | varchar(64) | CHECK length 1–64 |
| `status` | varchar(20) | CHECK in the 5 statuses |
| `stage`, `error_code`, `error_message`, `retryable`, `upstream_status` | | all set iff `status='failed'` (CHECK). Message ≤ 1000 chars (truncated by the app) |
| `error_issues` | jsonb, default `[]` | field/code/message, never values. CHECK is array |
| `destination_customer_id` | varchar(64) | |
| `fields_changed` | varchar(64)[] | field **names** |
| `mapping_version` | int | CHECK ≥ 1 |
| `source_last_modified`, `started_at`, `finished_at`, `recorded_at` | timestamptz | CHECK `finished_at >= started_at` |
| `duration_ms` | int | CHECK ≥ 0 |

Indexes, matching the three query paths of `list_recent`:
`(tenant_id, seq DESC)`, `(tenant_id, crm_account_id, seq DESC)`, and a partial
`(tenant_id, seq DESC) WHERE status='failed'`.

`tenant_id` is not yet a foreign key. A `tenants` table arrives with
authentication in M5.

### Concurrency lock lifecycle

> **Revised in M6.** M4 used `pg_advisory_xact_lock`, which kept the lock
> connection "idle in transaction" for the whole sync, including its HTTP calls.
> M6 switched to a **session-level** lock on an AUTOCOMMIT connection, so no
> transaction stays open (verified with `pg_stat_activity` in tests and on the
> live stack). The table below describes the original M4 design. The current steps are:
> autocommit connection → `SET lock_timeout` → `pg_advisory_lock(key)` →
> `SET idle_session_timeout` (backstop) → sync → `pg_advisory_unlock(key)` +
> `RESET` → pool. If unlock fails or returns false, the connection is
> **invalidated** (closed, which releases the lock) instead of pooled. See
> `app/services/locks.py`.

Original M4 implementation (`pg_advisory_xact_lock`), kept for reference:

| # | Step | On failure |
|---|---|---|
| 1 | Check out a pooled connection | `LockUnavailableError` → `failed/acquire_lock/database_unavailable` (HTTP 503) |
| 2 | `BEGIN`, `SET LOCAL lock_timeout = SYNC_LOCK_TIMEOUT_SECONDS`, `SET LOCAL idle_in_transaction_session_timeout = max(300 s, 2 × worst-case sync)` | same |
| 3 | `SELECT pg_advisory_xact_lock(key)`, `key = int64(sha256(tenant ∖0 account)[:8])`; waits for any current holder | SQLSTATE 55P03 → `LockTimeoutError` → `failed/acquire_lock/sync_lock_timeout` (HTTP 409, retryable) |
| 4 | Run the sync: CRM read, map, CMS writes, history insert | the body's exception propagates unchanged |
| 5 | `COMMIT` (success) or `ROLLBACK` (exception); PostgreSQL releases the xact lock automatically | commit errors are swallowed: the work is done, and a dead session already released its locks |
| 6 | Return the connection to the pool | — |
| — | Process crash / network loss | the server ends the session and releases the lock (tested with `pg_terminate_backend`) |

M4 chose transaction-scoped locks because there's no explicit unlock to forget.
M6 kept that safety property in a different way: any doubt about the unlock
means the connection is closed rather than pooled (tested by killing the lock
session mid-sync).

The server-side backstop (now `idle_session_timeout`) reclaims locks from a
hung process. It is deliberately set to at least 2× the computed worst-case
sync duration (`worst_case_sync_seconds`) so it can't fire during a legitimate
sync. A test holds a lock past a 1 s backstop and confirms the server frees it.

**Evidence:** `tests/integration/test_pg_locks.py` (4 separate OS processes:
critical sections never overlap and are strictly serialised; different
accounts run in parallel; timeout; release on exception; release on killed
session; fail closed when the DB is down) and
`test_pg_sync.py::test_concurrent_syncs_from_separate_processes` (6 processes
→ 1 `created`, 5 `unchanged`, 1 destination write, 6 history rows). With the
lock statement replaced by a no-op, both tests fail.

### Database unavailability

| Situation | Behaviour |
|---|---|
| DB down at lock time | No upstream calls. Record `acquire_lock/database_unavailable`. History write also fails → `HistoryPersistenceError` → HTTP 503, `X-Sync-History-Persisted: false`, record logged at ERROR |
| History write fails after a successful sync | HTTP 503 with the real outcome (e.g. `created`) in the body, header `false`, record logged in full (identifiers only). The lock is still released |
| `GET /sync/history` (or any route without its own handling) while DB down | HTTP 503 `{"detail": "database unavailable; retry later"}` (500 `database error` for non-connectivity failures) |
| DB restarted / connections killed | next checkout reconnects (`pool_pre_ping`) |
| `/health/ready` | 503 when the DB doesn't answer `SELECT 1`. `/health` (liveness) is unaffected |

## Multi-tenancy and authentication (Milestone 5)

```
 Bearer JWT ─▶ get_principal ─▶ TokenVerifier (JWKS cache) ─▶ tenants table (exists + active)
                    │
                    ▼  Principal{subject, tenant_id, roles, permissions}
              require(Permission.X) ─▶ route handler ─▶ service(principal.tenant_id, …)
                                                          │
             TenantConnectionProvider ◀───────────────────┤  per-tenant CRMClient + InternalSystemClient
               └─ tenant_integrations row + EnvSecretResolver (env:IL_<TENANT>__<NAME>)
             DbMappingProvider ◀──────────────────────────┘  active tenant_mapping_configs version
```

New tables (migration `0002`, additive): `tenants`, `tenant_integrations`
(connection URLs and **secret references**, operator-managed),
`tenant_mapping_configs` (append-only versions; active = highest). FK
`sync_records.tenant_id → tenants.id` (backfilled, added `NOT VALID` then validated).
Migration `0003` normalises CHECK constraint names (see ED-023).

Each tenant has its own CRM org and CMS instance. In compose: Meridian →
`mock-crm`/`mock-cms`, Harbor → `mock-crm-harbor`/`mock-cms-harbor`.
Clients are cached per (tenant, config + secret fingerprint), so rotating a
secret or editing a connection rebuilds the clients automatically.

`AccountSyncService` has one constructor: `connections`, `mappings`, `history`,
`locks`, all required. Unit tests pass test fakes (`StaticConnections`,
`InProcessSyncLocks`, in-memory repositories) from `tests/support/fakes.py`.
In production every tenant endpoint must be HTTPS.

## Concurrency lock: lost-lock mitigation (strategy; not yet implemented)

**The edge case (M4 risk 2).** The advisory lock lives on a dedicated
connection. If that connection ends mid-sync (the
`idle_in_transaction_session_timeout` backstop fires, an admin runs
`pg_terminate_backend`, a network partition, a database failover), PostgreSQL
releases the lock while the original worker keeps running. A second worker
can then take the lock, and two workers may write the same destination record.
The backstop is set to at least 2× the worst-case sync duration, so the
timeout path needs a hung process. The other paths remain.

**Why the lock alone can't fix this.** No lock that can expire (lease, session
or timeout) is safe on its own: a paused process can always resume after
losing it. The standard remedy is to make the *resource* reject stale writers
(fencing), with the lock as an efficiency measure.

**Mitigation, in order of value per unit of complexity:**

| # | Measure | Closes | Cost |
|---|---|---|---|
| 1 | **Lock-liveness check before every destination write.** On the lock connection, run `SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted AND pid = pg_backend_pid()`. If the connection is dead or the lock isn't held, abort with retryable `sync_lock_lost` before writing. | every case where the lock was lost *before* the write starts | ~25 lines in `locks.py` (a `still_held()` method) and two calls in `_load`. No schema change |
| 2 | **Optimistic concurrency at the destination (fencing).** The CMS exposes a per-customer `version`/ETag. Sync reads it, then PATCHes with `If-Match`. A `412` means someone else wrote: re-read and re-diff once, else record retryable `destination_conflict`. Creates are already fenced by the unique `external_crm_id` plus the idempotency key. | the residual race between check 1 and the write, and also writes from Meridian staff in the same window | mock CMS: `version` field + `If-Match` handling. Client: header plus a 412 error type. Service: one bounded re-diff |
| 3 | **Observability.** Log and metric when lock acquisition waits, the lock hold time approaches the backstop, or `sync_lock_lost` fires. Alert on PostgreSQL "terminating connection due to idle-in-transaction timeout". | detection | small |

Not recommended: a fencing-token table in our own database. It can stop a stale
worker writing *history*, but not writing the *destination*, which is where
the damage happens.

**Status after Milestone 7:** measure 1 is **implemented**
(`_PgLockHandle.verify()` runs immediately before each destination create or
update, and the sync fails with retryable `sync_lock_lost` without writing).
A worker-side processing deadline guards the same points (see below).
Measure 3 is partly implemented (`expired_leases` metric, `lease_expired`
attempt rows, `sync_lock_lost` / `processing_deadline_exceeded` error codes).
Measure 2 (destination fencing) is **not** implemented: the mock CMS has no
version/ETag support.

**Remaining limitation:** both checks run *immediately before* the HTTP write.
A lock or lease lost *during* the write itself (milliseconds to the client
timeout) isn't detected, and a stale worker could still complete that one
in-flight write. Its *outcome* can't overwrite the newer one (claim-token
fencing), and the sync is idempotent, so the realistic effect is a redundant
write with the same CRM-owned values. Only destination-side conditional writes
(measure 2) close this window completely. Implement 2 when the
destination supports conditional writes (most production APIs, including
Salesforce, expose ETags or version fields). With 2 in place the lock is purely
a performance optimisation and correctness no longer depends on it.

## Webhooks and background processing (Milestone 6)

```
 CRM (per tenant)                       IntegrationLab API                         PostgreSQL               Worker x N
 ────────────────                       ──────────────────                         ──────────               ──────────
 account updated
 POST /webhooks/crm/{tenant} ────────▶  1. read raw body (≤ 64 KiB)
   X-IntegrationLab-Signature:          2. tenant + integration → webhook secret(s)
   t=<unix>,v1=<hmac>                      (unknown tenant / no secret ≡ bad signature → 401)
                                        3. HMAC-SHA256("{t}.{body}") constant-time
                                        4. |now − t| ≤ 300 s, else 401 (replay)
                                        5. validate envelope; payload tenant must match
                                        6. INSERT … ON CONFLICT (tenant_id, source, event_id)
                                           DO NOTHING ─────────────────────────────▶ webhook_events
                                           COMMIT                                    status=pending
                      ◀──────────────── 7. 202 accepted | 200 duplicate | 409 conflict
                                                                                    ▲   claim (short txn):
                                                                                    │   SELECT … FOR UPDATE
                                                                                    │   SKIP LOCKED → UPDATE
                                                                                    │   status=processing,
                                                                                    │   claim_token, lease
                                                                                    │   COMMIT ─────────────▶ AccountSyncService
                                                                                    │                        (advisory lock,
                                                                                    │                         CRM/CMS HTTP, history)
                                                                                    └── complete (short txn,
                                                                                        fenced by claim_token):
                                                                                        succeeded | failed + error
```

**Inbox pattern.** The API only authenticates and persists. Processing
happens in `python -m app.worker`, a separate process unrelated to FastAPI's
request lifecycle (no `BackgroundTasks`). The sender gets a 2xx **only after
COMMIT**. If the database is unavailable it gets 503 and retries.

### Table `webhook_events` (migration `0004`)

| Column | Purpose |
|---|---|
| `id` uuid PK. `tenant_id` FK. `source` ('crm'). `event_id` (provider id) | **`UNIQUE (tenant_id, source, event_id)`**: durable, tenant-scoped idempotency |
| `event_type`, `crm_account_id`, `occurred_at`, `signed_at`, `received_at` | what/when. `signed_at` is the verified signature timestamp |
| `payload` jsonb | **allowlisted envelope only** (`event_id`, `event_type`, `occurred_at`, `account_id`). Never headers, signatures, secrets or extra fields |
| `payload_sha256` | hash of the raw body: same `event_id` + same body = duplicate, different body = 409 |
| `status` | `pending → processing → succeeded \| failed`. `ignored` for unsupported types |
| `attempts` | number of claims (bounds crash loops via `WORKER_MAX_ATTEMPTS`) |
| `available_at` | earliest claim time (M7 backoff will set it) |
| `claim_token`, `claimed_by`, `claimed_at`, `lease_expires_at` | lease. CHECK: `processing ⇔ claim_token & lease set` |
| `processed_at`, `last_error_code/_message/_retryable`, `sync_record_id` (FK) | outcome, linked to the resulting `sync_records` row |

Indexes: partial `(available_at, received_at) WHERE status='pending'` (claim),
partial `(lease_expires_at) WHERE status='processing'` (crash recovery),
`(tenant_id, received_at DESC)` (listing). `tenant_integrations` gained
`webhook_secret_ref` / `webhook_secret_previous_ref` (rotation).

### Worker guarantees

| Concern | Mechanism | Evidence |
|---|---|---|
| Two workers never take the same event | `FOR UPDATE SKIP LOCKED` in the claim | 4 OS processes × 30 events: every event `attempts = 1`. 8 threads × 40 claims: disjoint. Removing SKIP LOCKED makes both tests fail |
| No transaction during HTTP | claim and complete are separate short transactions. Lock connection is idle, not in a transaction | `pg_stat_activity` probed at every outbound HTTP request: 0 idle-in-transaction. Reverting the lock makes it fail |
| Worker crash | lease expires → reclaimed by any worker (`attempts + 1`) | real `SIGKILL` mid-event, then recovery by another worker |
| Crash loops | at `attempts ≥ WORKER_MAX_ATTEMPTS` an expired event becomes `failed / worker_attempts_exhausted` | tested |
| Stale worker after lease loss | completion requires the matching `claim_token` | stale completion while the new owner holds the lease is rejected (mutation-tested) |
| DB down at claim | worker logs, backs off (≤ 30 s), keeps running | tested |
| DB down at completion | event stays `processing`, is reclaimed after the lease, and re-runs idempotently (`unchanged`) | tested |
| Same-account events | per-account advisory lock (M4) serialises them | 30 events / 2 accounts / 4 processes → exactly 2 `created`, 28 `unchanged` |
| Bounded execution | client timeouts + bounded retries. Lease ≥ worst-case + lock wait (startup check). Small batches. SIGTERM finishes the current event | tested |
| Restarts | events are rows. Nothing is held in memory | new process drains events written before an engine/process restart. Live: API + DB container restart with workers stopped |

### Designed for Milestone 7 (not implemented)

A processing failure is currently a terminal `failed` with
`last_error_retryable` recorded. M7 will:
- turn retryable failures into `pending` with `available_at = now() + backoff(attempts)`;
- add a `dead_letter` status after a bounded number of attempts;
- add an authorized replay endpoint that moves `failed`/`dead_letter` back to `pending`.

The claim query already honours `available_at`. No schema change is needed
beyond widening the status CHECK.

## Retries, dead-letter and recovery (Milestone 7)

```
                 retryable & attempts < max: available_at = now + backoff(attempts)
            ┌───────────────────────────────────────────────────────────────┐
            ▼                                                               │
 pending ──claim──▶ processing ──complete──┬──▶ succeeded                    │
   ▲                    │                  ├──▶ failed       (permanent: never auto-retried)
   │                    │                  ├──▶ pending ─────────────────────┘
   │                    │                  └──▶ dead_letter  (retryable but attempts exhausted)
   │                    └── lease expired: reclaimed (attempt logged 'lease_expired');
   │                        at max attempts → dead_letter
   └──── POST /webhooks/events/{id}/replay (sync:run, audited): failed|dead_letter → pending, attempts = 0
```

**Retry policy** (`app/worker/retry_policy.py`):

| Outcome | attempts < `WORKER_MAX_ATTEMPTS` | attempts ≥ max |
|---|---|---|
| success | `succeeded` | `succeeded` |
| retryable failure (timeouts, 5xx/429, lock timeout/lost, deadline, history write) | `pending`, `available_at = now + delay` | `dead_letter` |
| permanent failure (not found, auth, mapping, 4xx, tenant inactive) | `failed` | `failed` |

`delay` for attempt *n* is uniform in `[c/2, c]` where `c = min(RETRY_MAX_DELAY_SECONDS,
RETRY_BASE_DELAY_SECONDS × 2^(n−1))` ("equal jitter": decorrelates workers,
guarantees minimum spacing, bounded). Defaults: base 5 s, cap 300 s, 5
attempts, giving a worst case of about 75 s of retry time before dead-letter. Crash
reclaims count as attempts too. The claim query was unchanged from M6: it already
honoured `available_at`.

**Completion writes the outcome and the attempt row in one statement**
(data-modifying CTE), fenced by `claim_token`. A stale worker's completion
matches no row, so neither its outcome nor its attempt record is stored.

**Safe processing deadline (req. 6).** The lease is ≥ the worst-case event
duration (M6 startup check). The worker records a monotonic time *before*
claiming, which is never later than the server's lease start. The guard passed into
the sync refuses any destination write once `now + write_margin ≥ t_claim + lease`,
where `write_margin` is the worst case of one destination call
(`(retries+1) × timeout + retries × backoff_cap`, 6 s with defaults). So a
stalled worker (GC pause, suspended VM, long lock wait) never *starts* a
write after another worker could have reclaimed the event. Lease *renewal*
wasn't added: with the worst-case-bounded lease it would only add moving parts.

**Lock-ownership verification (req. 7):** see "Concurrency lock: lost-lock mitigation" above.

### New tables (migration `0005`)

| Table | Purpose |
|---|---|
| `webhook_events` (+ columns) | status `dead_letter`; `dead_lettered_at`, `replay_count`, `last_replayed_at`, `last_replayed_by`. CHECKs: dead_letter needs a timestamp and an error |
| `webhook_event_attempts` | one row per attempt: number, worker, start/finish, outcome (`succeeded`, `retry_scheduled`, `failed`, `dead_letter`, `lease_expired`), error code/message/retryable, `next_attempt_at`, `sync_record_id`. Cascades with its event |
| `audit_log` | append-only: a trigger blocks UPDATE, DELETE and TRUNCATE. actor (token `sub` or `cli:<user>`), client id, tenant, action, target, outcome (`success`/`denied`/`rejected`/`error`), details (no secrets: forbidden keys rejected) |
| `worker_heartbeats` | worker id, host, pid, state (`idle`/`processing`/`backing_off`/`stopped`), last_seen, counters |

Audited actions: `webhook_event.replay` (success, rejected and **denied**),
`webhook_event.replay_bulk` (denied), `mapping.publish` (success, rejected,
denied) and `retention.purge`. Audit rows for replay are written in the same
transaction as the state change. Each record is also logged as one JSON line
(`integrationlab.audit`).

### Metrics

| Endpoint | Audience | Content |
|---|---|---|
| `GET /ops/metrics` | tenant users (`history:read`) | own tenant: pending, ready, retry_backlog, processing, expired_leases, failed, dead_letter, succeeded_1h, oldest pending age, p50/p95/max receive→success latency (1 h). Worker health as counts only |
| `GET /metrics` | platform (separate `METRICS_TOKEN`, disabled if unset) | Prometheus text, all tenants (`tenant` label), healthy workers |

A worker is healthy if its heartbeat is newer than the lease (it writes one
before every claim and when it starts each event).

### Retention (`python -m app.cli retention [--execute]`)

Deletes only `succeeded` events older than 30 d, `ignored` events older than
7 d, and sync records older than 90 d that no event references. Never:
pending/processing/failed/dead_letter events, the audit log, tenants or
configuration. Dry-run by default. `--execute` additionally requires
`RETENTION_ENABLED=true` (default false). Batches of 1000 in separate short
transactions with `SKIP LOCKED`. One audit record per tenant per run. **It has
not been executed against the development database.**
