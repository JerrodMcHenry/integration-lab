# API Contracts

Base URL (local): `http://localhost:8000`. Machine-readable schema:
`/openapi.json` (development only).

All endpoints except `/health*` and `POST /webhooks/crm/{tenant_id}` (HMAC-authenticated) require `Authorization: Bearer <access token>`.
See [security.md](security.md) for the token requirements and the permission model.

## Common responses

| Status | When | Body |
|---|---|---|
| 401 | missing, malformed, expired or invalid token | `{"detail": "<reason>"}` + `WWW-Authenticate: Bearer error="invalid_token", …` |
| 403 | missing permission | `{"detail": "missing permission sync:run"}` + `WWW-Authenticate: Bearer error="insufficient_scope", scope="…"` |
| 403 | tenant not provisioned or suspended | `{"detail": "tenant is not provisioned" \| "tenant is suspended"}` |
| 503 | IdP keys unavailable / database unavailable | `{"detail": "identity provider unavailable" \| "database unavailable; retry later"}` |

## Synchronization

### `POST /sync/accounts/{crm_account_id}` (requires `sync:run`)
`crm_account_id`: `^[A-Za-z0-9_-]{1,64}$`. Runs one sync for the caller's
tenant, using that tenant's CRM, CMS and mapping. Always returns a `SyncRecord`.

| Status | Meaning |
|---|---|
| 201 | `created` |
| 200 | `updated`, `unchanged` or `skipped_stale` |
| 404 | `crm_not_found` (not in **this tenant's** CRM) |
| 409 | `sync_lock_timeout` (another sync of this account is running) |
| 422 | `mapping_failed` / `mapping_config_invalid` |
| 500 | `tenant_connection_invalid` (integration row or secret missing/invalid), `unexpected_error` |
| 502 | (M7) also `sync_lock_lost` / `processing_deadline_exceeded`: ownership check refused the destination write (retryable) |
| 502 | upstream error (`crm_unavailable`, `destination_unavailable`, …) |
| 503 | `database_unavailable`, or history not persisted (`X-Sync-History-Persisted: false`) |
| 504 | upstream timeout |

Header `X-Sync-History-Persisted: true|false` is present on every response from this endpoint.

```json
{
  "id": "0b9f…", "tenant_id": "meridian", "crm_account_id": "SF-1002",
  "status": "updated", "stage": null, "error": null,
  "destination_customer_id": "CUS-65e7807734e7",
  "fields_changed": ["annual_revenue_usd", "crm_last_modified_at"],
  "mapping_version": 1, "source_last_modified": "2026-11-12T04:12:33+00:00",
  "started_at": "…", "finished_at": "…", "duration_ms": 41
}
```

Failure: `status: "failed"`, `stage ∈ {acquire_lock, fetch_source, transform,
load_destination, internal}`, `error: {code, message, retryable, upstream_status, issues[]}`.

### `GET /sync/history` (requires `history:read`)
Query: `limit` (1–500, default 50), `status`, `crm_account_id`. Returns the
caller's tenant's records, newest first.

### `GET /sync/history/{id}` (requires `history:read`)
404 if missing **or** owned by another tenant.

## Identity and tenant

| Endpoint | Permission | Response |
|---|---|---|
| `GET /me` | any authenticated | `{subject, tenant_id, roles[], permissions[]}` |
| `GET /tenant` | `tenant:read` | `{id, display_name, status}` |

## Administration (caller's tenant only)

| Endpoint | Permission | Notes |
|---|---|---|
| `GET /admin/integration` | `config:read` | `{crm_base_url, crm_client_id, crm_client_secret: "configured", cms_base_url, cms_api_key: "configured"}`. Secrets and secret references are never returned |
| `GET /admin/mapping` | `config:read` | active version: `{version, created_by, created_at, config}` |
| `GET /admin/mapping/versions` | `config:read` | `[{version, created_by, created_at}]`, newest first, max 100 |
| `PUT /admin/mapping` | `config:write` | body: mapping config ([integration-guide.md](integration-guide.md)). `tenant_id` must equal the caller's (else 422). Validated in full (422 with `detail.problems[]`). The server assigns `version = previous + 1`. 201 on success, 409 on a concurrent publish. Takes effect on the next sync, with no restart |

## Webhooks

### `POST /webhooks/crm/{tenant_id}` (HMAC-signed, no bearer token)

Headers: `Content-Type: application/json`,
`X-IntegrationLab-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`.
Body ≤ 64 KiB:

```json
{"event_id": "evt_3bce…", "event_type": "account.updated",
 "occurred_at": "2026-11-12T10:00:00Z", "data": {"account_id": "SF-1002"}}
```

`event_id`: `^[A-Za-z0-9._:-]{1,128}$`, unique per tenant. Supported types:
`account.created`, `account.updated`. Others are acknowledged and stored as `ignored`.

| Status | Body | Meaning |
|---|---|---|
| 202 | `{"status":"accepted","id":…,"event_id":…,"event_status":"pending"}` | persisted (committed). Will be processed |
| 202 | `{"status":"ignored",…}` | unsupported type, stored, no processing |
| 200 | `{"status":"duplicate","id":<original>,…,"event_status":<current>}` | same `event_id` and body already received. No new work |
| 401 | `{"detail":"signature verification failed"}` | bad/missing signature, unknown tenant, no secret configured (deliberately indistinguishable) |
| 401 | `{"detail":"signature timestamp outside tolerance"}` | valid signature but `t` older/newer than `WEBHOOK_TOLERANCE_SECONDS` |
| 403 | tenant suspended | |
| 409 | `{"status":"conflict",…}` | `event_id` reused with a different body |
| 413 | | body too large |
| 422 | | authenticated, but the payload is invalid or its `tenant_id` mismatches |
| 503 | `{"detail":"event store unavailable; retry later"}` | **not persisted.** Sender must retry |

### `GET /webhooks/events` (requires `history:read`)
Query: `limit` (1–500), `status` (`pending|processing|succeeded|failed|ignored`).
Returns the caller's tenant's events, newest first:
`{id, event_id, event_type, crm_account_id, status, attempts, occurred_at,
received_at, processed_at, last_error: {code, message, retryable} | null, sync_record_id}`.

### `GET /webhooks/events/{id}` (requires `history:read`)
404 if missing or owned by another tenant. Since M7 the response also has
`available_at`, `dead_lettered_at`, `replay_count` and `last_replayed_at`.
`status` may be `dead_letter`.

### `GET /webhooks/events/{id}/attempts` (requires `history:read`)
Retry history, oldest first: `[{attempt, worker_id, started_at, finished_at,
outcome: succeeded|retry_scheduled|failed|dead_letter|lease_expired, error_code,
error_message, retryable, next_attempt_at, sync_record_id}]`. Attempt numbers restart at 1 after a replay.

### `POST /webhooks/events/{id}/replay` (requires `sync:run`, audited)
Body (optional): `{"reason": "≤ 500 chars"}`.
| Status | Meaning |
|---|---|
| 202 | `{"status":"replayed","id":…,"previous_status":"failed"\|"dead_letter","new_status":"pending"}`. Fresh attempt budget |
| 403 | missing `sync:run` (**recorded in the audit log as `denied`**) |
| 404 | no such event in your tenant |
| 409 | event is `pending`, `processing`, `succeeded` or `ignored` (audited as `rejected`) |
| 503 | event store unavailable |

### `POST /webhooks/events/replay` (requires `sync:run`, audited)
Body: `{"statuses": ["dead_letter"] | ["failed"] | both, "limit": 1–100 (default 50), "reason": "…"}`.
Replays the oldest matching events in your tenant. Response `{"replayed": n, "ids": [...]}`.
One audit record per event.

## Operations

| Endpoint | Permission | Response |
|---|---|---|
| `GET /ops/metrics` | `history:read` | `{tenant_id, events: {pending, ready, retry_backlog, processing, expired_leases, failed, dead_letter, succeeded_1h, oldest_pending_s, latency_p50_s, latency_p95_s, latency_max_s}, workers: {healthy, total_seen, last_heartbeat_at}}` |
| `GET /admin/audit?limit=&action=` | `config:read` | `[{occurred_at, actor, actor_client_id, action, outcome, target_type, target_id, details}]`, newest first |
| `GET /metrics` | `Authorization: Bearer $METRICS_TOKEN` (not a user JWT). 404 when unset | Prometheus text: `integrationlab_events_{pending,ready,retry_backlog,processing,expired_leases,failed,dead_letter,succeeded_1h,oldest_pending_s,latency_p95_s}{tenant=…}`, `integrationlab_workers_healthy` |

## Health (public)

| Endpoint | Response |
|---|---|
| `GET /health` | liveness `{"status": "healthy", …}` |
| `GET /health/ready` | 200 `{"status":"ready","database":"ok"}` or 503 |
