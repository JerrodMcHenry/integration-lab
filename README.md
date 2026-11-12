# IntegrationLab

A portfolio proof-of-concept enterprise integration platform. It synchronizes
customer accounts for the fictional financial-services firm **Meridian
Capital** from a Salesforce-style CRM into Meridian's internal Customer
Management System.

> **Status: work in progress — Milestone 4 of 10 complete.** This is not
> production software: the API has no authentication yet (M5). See
> [docs/architecture.md](docs/architecture.md) for what is implemented and
> what is planned.

## What works today

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
- **Two mock services** with fault injection. Admin routes are disabled in production.

## Run everything with Docker Compose

```bash
cp .env.example .env
# set POSTGRES_PASSWORD in .env, e.g. the output of: openssl rand -hex 24

docker compose up -d --build --wait   # db → migrate (alembic upgrade head) → mocks → api (4 workers)
curl -X POST localhost:8000/sync/accounts/SF-1002
curl localhost:8000/sync/history
curl localhost:8000/health/ready
docker compose stop                   # stop; data is kept in the integrationlab_pgdata volume
```

All ports are bound to `127.0.0.1` only: API 8000, mock CRM 8001, mock CMS
8002, PostgreSQL 5434. OpenAPI docs: http://localhost:8000/docs

## Local development (app on the host, DB in Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # set POSTGRES_PASSWORD

docker compose up -d --wait db
alembic upgrade head                  # uses DATABASE_URL from .env

uvicorn mock_crm.main:app --port 8001        # terminal 1
uvicorn mock_internal.main:app --port 8002   # terminal 2
uvicorn app.main:app --port 8000             # terminal 3
```

Without `DATABASE_URL`, the app falls back to in-memory history and
in-process locks and logs a warning (development only; production refuses to start).

## Tests

```bash
docker compose up -d --wait db
pytest                       # unit + PostgreSQL integration tests
REQUIRE_POSTGRES=1 pytest    # fail (instead of skip) if PostgreSQL is unreachable
pytest -m "not postgres"     # unit tests only
```

Integration tests use `TEST_DATABASE_URL` (database `integrationlab_test`,
created automatically) and refuse to run against a database whose name
doesn't end in `_test`.

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

- [Architecture](docs/architecture.md): includes persistence, schema and the lock lifecycle
- [Customer requirements](docs/customer-requirements.md)
- [Integration guide](docs/integration-guide.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Engineering decisions](docs/engineering-decisions.md)

API contracts and the demo script will be added as the features they describe are built.
