# Architecture

> Status legend: ✅ implemented and tested · ⚠️ implemented with a documented limitation · 🚧 planned (not built)

## Context

IntegrationLab synchronizes customer account records for **Meridian Capital**
from a Salesforce-style CRM into Meridian's internal Customer Management
System (CMS). Both external systems are simulated by separate mock services.

## Current state (Milestone 3 complete)

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
| Sync history | `app/repositories/sync_history.py` | ⚠️ in-memory, not durable |
| Sync API | `app/api/sync.py` | ⚠️ unauthenticated, single tenant |
| Mock CRM / mock CMS / admin guard | `mock_crm/`, `mock_internal/`, `mock_common/` | ✅ |
| PostgreSQL + SQLAlchemy + Alembic, Docker Compose | — | 🚧 M4 |
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
   history and returned.

### Result model

`status ∈ {created, updated, unchanged, skipped_stale, failed}`. Failures carry
`stage ∈ {fetch_source, transform, load_destination, internal}` and an `error`
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
