# Architecture

> Status legend: ✅ implemented and tested · ⚠️ implemented with a documented limitation · 🚧 planned (not built)

## Context

IntegrationLab synchronizes customer account records for **Meridian Capital**
from a Salesforce-style CRM into Meridian's internal Customer Management
System (CMS). Both external systems are simulated by separate mock services.

## Current state (Milestone 4 complete)

```
                     ┌──────────────────────────── IntegrationLab (FastAPI :8000) ───────────────────────────┐
 POST /sync/accounts │  api/sync.py ──▶ services/account_sync.py (orchestration only)                         │
 GET  /sync/history  │                   │            │                 │                     │               │
                     │                   ▼            ▼                 ▼                     ▼               │
                     │   integrations/crm_client  mapping/mapper  integrations/internal_client  repositories/  │
                     │   (OAuth2 CC, retries)     (JSON config,   (API key, idempotent         sync_history   │
                     │                            whitelisted     creates, retries)            (Protocol +    │
                     │                            transforms)                                   in-memory)    │
                     └───────────│──────────────────────────────────────────│────────────────────────────────┘
                                 ▼                                           ▼
                     Mock CRM (:8001)                              Mock Meridian CMS (:8002)
                     OAuth token endpoint, expiring tokens        customers keyed by external_crm_id
                     GET/PATCH accounts                           POST (Idempotency-Key, 409 on dup), PATCH
                     /_admin faults (dev only)                    /_admin faults (dev only)
```

| Component | Path | Status |
|---|---|---|
| Health, `POST /accounts` (validation only, no persistence) | `app/main.py` | ✅ |
| Environment config, production safety checks | `app/core/config.py` | ✅ |
| CRM client | `app/integrations/crm_client.py` | ✅ |
| Internal CMS client | `app/integrations/internal_client.py` | ✅ |
| Shared error base + bounded retry | `app/integrations/errors.py`, `retry.py` | ✅ |
| Destination contract & field ownership | `app/schemas/internal_customer.py` | ✅ |
| Customer-specific field mapping | `app/mapping/` | ✅ file-based |
| Sync service | `app/services/account_sync.py` | ✅ |
| Sync history | `app/repositories/sync_history.py` | ✅ PostgreSQL (in-memory fallback for tests/local) |
| Per-account concurrency lock | `app/services/locks.py` | ✅ PostgreSQL advisory locks (in-process fallback) |
| Schema & migrations | `app/db/models.py`, `migrations/` | ✅ Alembic `0001` |
| Docker Compose stack | `docker-compose.yml`, `Dockerfile` | ✅ |
| Sync API | `app/api/sync.py` | ⚠️ unauthenticated, single tenant |
| Mock CRM / mock CMS / admin guard | `mock_crm/`, `mock_internal/`, `mock_common/` | ✅ |
| Tenants, OIDC auth, RBAC | — | 🚧 M5 |
| Webhooks, durable job queue, DLQ, replay | — | 🚧 M6–7 |
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

- **Wiring:** `DATABASE_URL` set → `PostgresSyncHistoryRepository` +
  `PostgresAdvisorySyncLocks`. Unset → in-memory history + in-process locks,
  with a startup WARNING. Production refuses to start without `DATABASE_URL`.
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

Implemented in `PostgresAdvisorySyncLocks.hold(tenant_id, crm_account_id)`:

| # | Step | On failure |
|---|---|---|
| 1 | Check out a pooled connection | `LockUnavailableError` → `failed/acquire_lock/database_unavailable` (HTTP 503) |
| 2 | `BEGIN`, `SET LOCAL lock_timeout = SYNC_LOCK_TIMEOUT_SECONDS`, `SET LOCAL idle_in_transaction_session_timeout = max(300 s, 2 × worst-case sync)` | same |
| 3 | `SELECT pg_advisory_xact_lock(key)`, `key = int64(sha256(tenant ∖0 account)[:8])`; waits for any current holder | SQLSTATE 55P03 → `LockTimeoutError` → `failed/acquire_lock/sync_lock_timeout` (HTTP 409, retryable) |
| 4 | Run the sync: CRM read, map, CMS writes, history insert | the body's exception propagates unchanged |
| 5 | `COMMIT` (success) or `ROLLBACK` (exception); PostgreSQL releases the xact lock automatically | commit errors are swallowed: the work is done, and a dead session already released its locks |
| 6 | Return the connection to the pool | — |
| — | Process crash / network loss | the server ends the session and releases the lock (tested with `pg_terminate_backend`) |

Why transaction-scoped instead of session-scoped `pg_advisory_lock`: there's
no explicit unlock to forget, and a pooled connection can never be handed back
still holding a lock.

The idle-in-transaction backstop reclaims locks from a hung process. It is
deliberately set to at least 2× the computed worst-case sync duration
(`worst_case_sync_seconds`) so it can't fire during a legitimate sync.

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
| `GET /sync/history` while DB down | HTTP 503 `{"detail": "Sync history storage unavailable"}` |
| DB restarted / connections killed | next checkout reconnects (`pool_pre_ping`) |
| `/health/ready` | 503 when the DB doesn't answer `SELECT 1`. `/health` (liveness) is unaffected |
