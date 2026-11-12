# Integration Guide

## CRM connection

IntegrationLab authenticates to the CRM with **OAuth 2.0 client credentials**
and calls its REST API with a bearer token.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `CRM_BASE_URL` | `http://127.0.0.1:8001` | CRM REST base URL |
| `CRM_TOKEN_URL` | `${CRM_BASE_URL}/oauth/token` | Token endpoint |
| `CRM_CLIENT_ID` | `integrationlab-dev` | Dev default only |
| `CRM_CLIENT_SECRET` | `dev-only-secret` | Dev default only. Must be overridden outside local dev |
| `CRM_TIMEOUT_SECONDS` | `5` | Per-request timeout (connect + read) |
| `CRM_MAX_RETRIES` | `2` | Retries for transient read failures, 0–5 |
| `CRM_BACKOFF_BASE_SECONDS` | `0.5` | First backoff ceiling, doubles per attempt |
| `CRM_BACKOFF_MAX_SECONDS` | `4` | Backoff cap |

CRM credentials are process-wide for now. Per-tenant credentials move to the
database in M4/M5.

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
| `INTERNAL_API_BASE_URL` | `http://127.0.0.1:8002` | |
| `INTERNAL_API_KEY` | `dev-only-internal-key` | Bearer API key. Dev default, rejected in production |
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

One JSON file per tenant in `app/mapping/configs/<tenant_id>.json` (directory
overridable with `MAPPING_CONFIG_DIR`). See `meridian.json` and
[customer-requirements.md](customer-requirements.md).

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
