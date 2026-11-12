# IntegrationLab

A portfolio proof-of-concept enterprise integration platform. It synchronizes
customer accounts for the fictional financial-services firm **Meridian
Capital** from a Salesforce-style CRM into Meridian's internal Customer
Management System.

> **Status: work in progress — Milestone 7 of 10 complete.** This is not
> production software (see the remaining risks in [docs/security.md](docs/security.md)). See
> [docs/architecture.md](docs/architecture.md) for what is implemented and
> what is planned.

## What works today

- **Event-driven sync:** the CRM sends signed webhooks, which are verified
  (HMAC over the raw body, 5-minute replay window), persisted before
  acknowledgement, deduplicated per tenant, and processed by background workers
  using `FOR UPDATE SKIP LOCKED` with crash-safe leases.
- **Reliable recovery:** transient failures retry with bounded exponential
  backoff and jitter, exhausted events go to dead-letter, operators replay with
  full attempt history, and sensitive actions land in an append-only audit log.
  Tenant metrics plus a Prometheus endpoint, and an opt-in retention policy.
- **On-demand account sync** (`POST /sync/accounts/{id}`): CRM → mapping →
  internal CMS, with create, update (changed fields only), unchanged
  (no write) and stale-skip outcomes.
- **Field ownership:** Salesforce owns account data. The CMS owns onboarding
  status and risk classification, and sync never writes those.
- **Customer-specific field mapping** as validated JSON with whitelisted
  transforms (no code execution).
- **Durable sync history in PostgreSQL** (`GET /sync/history`): outcome,
  stage, error code, retryability and field names. No account data is stored.
- **Cross-process concurrency safety:** PostgreSQL advisory locks serialise
  syncs per (tenant, account) across workers and hosts.
- **Resilient clients:** OAuth 2.0 client credentials (CRM), API key (CMS),
  typed errors, bounded jittered retries, idempotent creates.
- **Multi-tenant with OIDC + RBAC:** JWT access tokens are verified (signature,
  issuer, audience, expiry, `typ`), the tenant comes only from the token, and
  viewer/operator/admin permissions are enforced. Each tenant has its own CRM/CMS
  connections, secrets and versioned mappings.
- **Mock services** for two tenants' CRMs and CMSs plus a dev-only OIDC
  provider. Admin routes are disabled, and the mock IdP refuses to start, in production/staging.

## Run everything with Docker Compose

```bash
cp .env.example .env
# set POSTGRES_PASSWORD in .env, e.g. the output of: openssl rand -hex 24

docker compose up -d --build --wait   # db → migrate + seed → mocks + IdP → api (4 procs) + worker x2

# Get a token from the mock IdP (users: meridian-{admin,operator,viewer}, harbor-{admin,operator})
TOKEN=$(curl -s localhost:8090/oauth/token -d grant_type=password -d client_id=integrationlab-cli \
  -d username=meridian-operator -d password=dev-only-password | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -X POST -H "Authorization: Bearer $TOKEN" localhost:8000/sync/accounts/SF-1002
curl -H "Authorization: Bearer $TOKEN" localhost:8000/sync/history
curl -H "Authorization: Bearer $TOKEN" localhost:8000/me
curl localhost:8000/health/ready

# Event-driven: change an account in Meridian's CRM; the CRM sends a signed webhook, a worker syncs it
CRM=$(curl -s -u integrationlab-dev:dev-only-secret -d grant_type=client_credentials localhost:8001/oauth/token \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -X PATCH -H "Authorization: Bearer $CRM" -H 'Content-Type: application/json' \
  -d '{"AnnualRevenue": 9000000}' localhost:8001/crm/accounts/SF-1002
curl -H "Authorization: Bearer $TOKEN" localhost:8000/webhooks/events      # -> succeeded
curl -H "Authorization: Bearer $TOKEN" localhost:8000/ops/metrics           # queue, retries, dead letters, latency
curl -H "Authorization: Bearer $TOKEN" 'localhost:8000/webhooks/events?status=dead_letter'
curl -X POST -H "Authorization: Bearer $TOKEN" localhost:8000/webhooks/events/<id>/replay
docker compose stop                   # stop; data is kept in the integrationlab_pgdata volume
```

All ports are bound to `127.0.0.1` only: API 8000, IdP 8090, Meridian CRM/CMS
8001/8002, Harbor CRM/CMS 8011/8012, PostgreSQL 5434. OpenAPI docs: http://localhost:8000/docs

## Local development (app on the host, DB in Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # set POSTGRES_PASSWORD

docker compose up -d --wait db
alembic upgrade head                  # uses DATABASE_URL from .env
python -m app.cli seed-dev            # insert dev tenants if absent (never overwrites)

uvicorn mock_idp.main:app --port 8090        # identity provider
uvicorn mock_crm.main:app --port 8001        # Meridian CRM
uvicorn mock_internal.main:app --port 8002   # Meridian CMS
uvicorn app.main:app --port 8000
python -m app.worker                         # background event worker
```

PostgreSQL is required: the API, worker and CLI refuse to start without `DATABASE_URL`.

## Tests

```bash
docker compose up -d --wait db
pytest                       # unit + PostgreSQL integration tests
REQUIRE_POSTGRES=1 pytest    # fail (instead of skip) if PostgreSQL is unreachable
pytest -m "not postgres"     # unit tests only
```

Integration tests use only `TEST_DATABASE_URL` (database `integrationlab_test`,
created automatically). The test process never sees the development
`DATABASE_URL`: it's replaced by an unroutable sentinel, so a test can't reach
the dev database by accident. The run aborts if the test database isn't
PostgreSQL, isn't named `*_test`, or is the development database.

## Database migrations

| Task | Command |
|---|---|
| Apply all migrations | `alembic upgrade head` (or `docker compose run --rm migrate`) |
| Show current revision | `alembic current` |
| Show history | `alembic history --verbose` |
| Preview SQL without applying | `alembic upgrade head --sql` |
| New migration | `alembic revision -m "describe change"`, then write `upgrade()` / `downgrade()` by hand and add a test |

Migrations are additive and forward-only by default. Downgrading `0001` drops
all sync history and refuses to run unless `ALLOW_DESTRUCTIVE_DOWNGRADE=1` is set.
Only do that with explicit approval. Concurrent `upgrade` runs are serialised
by an advisory lock.

## Documentation

**New here? Start with [docs/codebase-guide.md](docs/codebase-guide.md)** (15–20 min).
Engineering rules for contributors and AI agents: [CLAUDE.md](CLAUDE.md).

- [Architecture](docs/architecture.md): includes persistence, the lock lifecycle, multi-tenancy and the lost-lock mitigation
- [API contracts](docs/api-contracts.md)
- [Customer requirements](docs/customer-requirements.md)
- [Integration guide](docs/integration-guide.md)
- [Security](docs/security.md): authentication flow, permission model, isolation, remaining risks
- [Troubleshooting](docs/troubleshooting.md)
- [Engineering decisions](docs/engineering-decisions.md)

The demo script will be added with the dashboard.
