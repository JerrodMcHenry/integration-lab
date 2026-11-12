# IntegrationLab

A portfolio proof-of-concept enterprise integration platform. It synchronizes
customer accounts for the fictional financial-services firm **Meridian
Capital** from a Salesforce-style CRM into Meridian's internal Customer
Management System.

> **Status: work in progress — Milestone 3 of 10 complete.** This is not
> production software: the API has no authentication yet and sync history is
> in memory. See [docs/architecture.md](docs/architecture.md) for what is
> implemented and what is planned.

## What works today

- **On-demand account sync** (`POST /sync/accounts/{id}`): CRM → mapping →
  internal CMS, with create, update (changed fields only), unchanged
  (no write) and stale-skip outcomes.
- **Field ownership:** Salesforce owns account data. The CMS owns onboarding
  status and risk classification, and sync never writes those.
- **Customer-specific field mapping** as validated JSON with whitelisted
  transforms (no code execution).
- **Clear results:** every attempt returns and records a `SyncRecord` with
  status, failure stage, error code, retryability and changed field names
  (`GET /sync/history`).
- **Resilient clients:** OAuth 2.0 client credentials (CRM), API key (CMS),
  typed errors, bounded jittered retries, idempotent creates.
- **Two mock services** with fault injection. Admin routes are disabled in production.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

uvicorn mock_crm.main:app --port 8001        # terminal 1
uvicorn mock_internal.main:app --port 8002   # terminal 2
uvicorn app.main:app --port 8000             # terminal 3

curl -X POST localhost:8000/sync/accounts/SF-1002
curl localhost:8000/sync/history
pytest
```

OpenAPI docs: http://localhost:8000/docs

## Documentation

- [Architecture](docs/architecture.md)
- [Customer requirements](docs/customer-requirements.md)
- [Integration guide](docs/integration-guide.md)
- [Security](docs/security.md)
- [Engineering decisions](docs/engineering-decisions.md)

API contracts, troubleshooting and the demo script will be added as the
features they describe are built.
