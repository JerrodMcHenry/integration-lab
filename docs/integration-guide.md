# Integration Guide

## CRM connection

IntegrationLab authenticates to the CRM with **OAuth 2.0 client credentials**
and calls its REST API with a bearer token.

### Configuration

Connection details are **per tenant** (`tenant_integrations`: CRM base URL, token
URL, client id and a *reference* to the client secret). Environment variables
set only the platform-wide HTTP policy:

| Variable | Default | Notes |
|---|---|---|
| `CRM_TIMEOUT_SECONDS` | `5` | Per-request timeout (connect + read) |
| `CRM_MAX_RETRIES` | `2` | Retries for transient read failures, 0–5 |
| `CRM_BACKOFF_BASE_SECONDS` | `0.5` | First backoff ceiling, doubles per attempt |
| `CRM_BACKOFF_MAX_SECONDS` | `4` | Backoff cap |

In production every tenant endpoint (CRM base URL, token URL, CMS base URL) must
be `https://`; a non-HTTPS endpoint fails the sync with `tenant_connection_invalid`.
The global `CRM_BASE_URL`, `CRM_TOKEN_URL`, `CRM_CLIENT_ID`, `CRM_CLIENT_SECRET`,
`INTERNAL_API_BASE_URL` and `INTERNAL_API_KEY` variables were removed in Phase 3;
if present they are ignored.

### Onboarding a tenant (operator task)

1. Add the tenant to `seed/dev_tenants.json` (dev) or insert rows into
   `tenants` and `tenant_integrations` (other environments). Tenant ids:
   lowercase letters/digits separated by single hyphens.
2. Provide secrets as env vars in the tenant's namespace:
   `IL_<TENANT>__CRM_CLIENT_SECRET`, `IL_<TENANT>__CMS_API_KEY` (`-` → `_` in
   the tenant segment, `__` before the name). The integration row references them as
   `env:IL_<TENANT>__CRM_CLIENT_SECRET`. A tenant can't reference another tenant's namespace.
3. Publish the first mapping (`PUT /admin/mapping` as the tenant's admin, or via seed).
4. Configure the IdP to issue tokens with `tenant_id=<tenant>` and `roles`.

### Endpoints consumed

| Method | Path | Purpose |
|---|---|---|
| POST | `/oauth/token` | `grant_type=client_credentials`, HTTP Basic client auth |
| GET | `/crm/accounts/{id}` | Retrieve account |

### CRM account payload (Salesforce-style)

```json
{
  "Id": "SF-1001",
  "Name": "Acme Capital",
  "Industry": "Financial Services",
  "AnnualRevenue": 2500000,
  "OwnerName": "Sarah Johnson",
  "LastModifiedDate": "2026-11-01T12:00:00+00:00"
}
```

## Internal Customer Management System (destination)

| Variable | Default | Notes |
|---|---|---|
| `INTERNAL_API_TIMEOUT_SECONDS` | `5` | |
| `INTERNAL_API_MAX_RETRIES` | `2` | 0–5. Applies to GET, PATCH and idempotency-keyed POST |
| `INTERNAL_API_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | `0.5` / `4` | |

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/customers/by-external-id/{external_crm_id}` | Look up customer linked to a CRM account (404 → not yet created) |
| POST | `/api/v1/customers` | Create. Honours `Idempotency-Key`, `409` if the external id already exists |
| PATCH | `/api/v1/customers/{customer_id}` | Partial update. Accepts any field, including internal-owned ones |
| GET | `/api/v1/customers`, `/api/v1/customers/{customer_id}` | Inspection |

## Field mapping configuration

Stored per tenant in the database (`tenant_mapping_configs`, versioned) and
managed with `GET/PUT /admin/mapping` (admin role). The files in
`app/mapping/configs/` (`meridian.json`, `harbor.json`) are seed input. See
[customer-requirements.md](customer-requirements.md) and
[api-contracts.md](api-contracts.md).

```json
{"source": "AnnualRevenue", "target": "annual_revenue_usd", "required": false,
 "transforms": [{"name": "to_decimal", "args": {"places": 2}}]}
```

- `source`: CRM field name or dotted path (max depth 3).
- `target`: must be a **CRM-owned** destination field. `onboarding_status`,
  `risk_classification` and system fields are rejected.
- `external_crm_id`, `legal_name` and `relationship_manager` must be mapped.
- Transforms (applied in order, `null` passes through except for `default`):
  `strip`, `collapse_whitespace`, `upper`, `lower`, `title`,
  `truncate{max_length}`, `to_decimal{places}`, `iso_datetime`,
  `value_map{values, default?, strict?}`, `default{value}`.
- The whole file is validated on first use. Any problem rejects the entire
  mapping and every problem is listed in the error.

## Synchronization API

All calls need `Authorization: Bearer <token>` (see [api-contracts.md](api-contracts.md)).

| Method | Path | Result |
|---|---|---|
| POST | `/sync/accounts/{crm_account_id}` | `SyncRecord`. 201 created · 200 updated/unchanged/skipped_stale · 404 not in CRM · 422 mapping failure · 502 upstream error · 504 upstream timeout |
| GET | `/sync/history?limit=&status=&crm_account_id=` | Recent `SyncRecord`s for the caller's tenant |
| GET | `/sync/history/{id}` | One record (404 if it belongs to another tenant) |

Example failure body:

```json
{"status": "failed", "stage": "load_destination",
 "error": {"code": "destination_unavailable", "message": "Internal API unavailable for customer:ext:SF-1002",
           "retryable": true, "upstream_status": 503, "issues": []},
 "crm_account_id": "SF-1002", "tenant_id": "meridian", "fields_changed": [], "...": "..."}
```

Error codes: `crm_not_found`, `crm_auth_failed`, `crm_bad_request`,
`crm_invalid_response`, `crm_timeout`*, `crm_unavailable`*, `mapping_failed`,
`mapping_config_invalid`, `destination_auth_failed`, `destination_not_found`,
`destination_conflict`, `destination_rejected_payload`,
`destination_invalid_response`, `destination_timeout`*,
`destination_unavailable`*, `unexpected_error` (* = retryable).

## Running the mock CRM locally

```bash
source .venv/bin/activate
uvicorn mock_crm.main:app --port 8001
```

Mock-only controls. These are mounted only when `APP_ENV` is development or
test, never in production/staging:

```bash
# Next 3 account reads return 503
curl -X PUT localhost:8001/_admin/faults -H 'Content-Type: application/json' \
  -d '{"mode":"error","remaining":3,"status_code":503}'
# Next read hangs 10 s (exceeds the default 5 s client timeout)
curl -X PUT localhost:8001/_admin/faults -H 'Content-Type: application/json' \
  -d '{"mode":"timeout","remaining":1,"delay_seconds":10}'
# Replace a raw account (e.g. malformed data for a mapping-failure demo)
curl -X PUT localhost:8001/_admin/accounts/SF-BAD -H 'Content-Type: application/json' -d '{"Name": ""}'
# Restore seed data, clear tokens and faults
curl -X POST localhost:8001/_admin/reset
```

The mock CMS (`uvicorn mock_internal.main:app --port 8002`) has the same
`/_admin/faults` (plus `"writes_only": true`) and `/_admin/reset`.

Updating an account (as a CRM user would):

```bash
TOKEN=$(curl -s -u integrationlab-dev:dev-only-secret \
  -d grant_type=client_credentials localhost:8001/oauth/token | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -X PATCH localhost:8001/crm/accounts/SF-1002 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"AnnualRevenue": 9000000}'
```

## Using the client in code

```python
from app.integrations.crm_client import CRMClient, CRMError

with CRMClient() as crm:
    try:
        account = crm.get_account("SF-1001")
    except CRMError as exc:
        if exc.retryable:
            ...  # schedule a retry
        else:
            ...  # record a permanent failure
```


## Receiving CRM webhooks (Milestone 6)

Configure the CRM (per tenant) to POST account changes to:

    https://<integrationlab-host>/webhooks/crm/<tenant_id>

Signing (sender side):

```python
import hmac, hashlib, json, time
body = json.dumps({"event_id": "evt_123", "event_type": "account.updated",
                   "occurred_at": "2026-11-12T10:00:00Z", "data": {"account_id": "SF-1002"}}).encode()
t = int(time.time())
sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
headers = {"Content-Type": "application/json", "X-IntegrationLab-Signature": f"t={t},v1={sig}"}
# POST exactly `body` (the bytes that were signed)
```

- Retry on 5xx and network errors with the **same body and `event_id`** (re-sign
  with a fresh `t`). Duplicates return 200 and don't cause extra processing.
- Never reuse an `event_id` for different content (409).
- The event only says *which* account changed. IntegrationLab re-reads the
  account from the CRM, so events may arrive out of order or be coalesced safely.
- Secret: `IL_<TENANT>__WEBHOOK_SECRET` (referenced by
  `tenant_integrations.webhook_secret_ref`). To rotate: set the new secret, point
  `webhook_secret_previous_ref` at the old one, switch the sender, then clear the previous ref.

### Worker

```bash
python -m app.worker                      # run until SIGTERM (finishes current event)
python -m app.worker --once               # one batch
docker compose up -d --scale worker=4     # more workers; SKIP LOCKED shares the queue
```

| Variable | Default | Meaning |
|---|---|---|
| `WORKER_BATCH_SIZE` | 10 | events claimed per round trip (lower = fairer spread across workers) |
| `WORKER_POLL_INTERVAL_SECONDS` | 1 | idle poll interval |
| `WORKER_LEASE_SECONDS` | 2 × worst case (≈ 214 s) | claim reservation. The worker refuses a value below the worst-case event duration |
| `WORKER_MAX_ATTEMPTS` | 5 | attempts (incl. crash reclaims) before `dead_letter` |
| `RETRY_BASE_DELAY_SECONDS` | 5 | backoff ceiling for retry *n* = min(cap, base·2^(n−1)); delay ∈ [ceiling/2, ceiling] |
| `RETRY_MAX_DELAY_SECONDS` | 300 | backoff cap |
| `METRICS_TOKEN` | unset (disabled) | bearer token for platform `/metrics` |
| `RETENTION_ENABLED` | false | must be true for `retention --execute` to delete |
| `RETENTION_SUCCEEDED_EVENT_DAYS` / `_IGNORED_EVENT_DAYS` / `_SYNC_RECORD_DAYS` | 30 / 7 / 90 | retention windows |
| `WEBHOOK_TOLERANCE_SECONDS` | 300 | replay window |
| `WEBHOOK_MAX_BODY_BYTES` | 65536 | |

The mock CRMs send webhooks automatically on `PATCH /crm/accounts/{id}`.
`GET /_admin/webhooks` shows deliveries and
`POST /_admin/webhooks/redeliver/{event_id}` resends one (dev only).


## Recovering from failures (Milestone 7)

1. **Transient errors** (CRM/CMS timeouts, 5xx, 429, lock contention) retry
   automatically with backoff. Nothing to do.
2. Events that still fail after `WORKER_MAX_ATTEMPTS` land in **`dead_letter`**.
   Permanent errors (unknown account, mapping errors, auth) land in **`failed`**
   right away.
3. Find them: `GET /webhooks/events?status=dead_letter` (or `failed`). Inspect
   `GET /webhooks/events/{id}/attempts` for the per-attempt errors.
4. Fix the cause (outage over, CRM data corrected, mapping published), then replay:
   ```bash
   curl -X POST -H "Authorization: Bearer $OPERATOR_TOKEN" -H 'Content-Type: application/json' \
     -d '{"reason":"INC-4711 resolved"}' localhost:8000/webhooks/events/<id>/replay
   # or, after a broad outage (max 100 per call):
   curl -X POST -H "Authorization: Bearer $OPERATOR_TOKEN" -H 'Content-Type: application/json' \
     -d '{"statuses":["dead_letter"],"limit":100,"reason":"CMS outage"}' localhost:8000/webhooks/events/replay
   ```
5. Replays are idempotent (the sync re-reads the CRM and only writes changes)
   and recorded in `GET /admin/audit`.

## Retention

```bash
python -m app.cli retention                       # dry run: counts what the policy would delete
RETENTION_ENABLED=true python -m app.cli retention --execute   # deletes (audited); needs approval
```
