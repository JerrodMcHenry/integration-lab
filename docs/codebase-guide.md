# Codebase Guide

A 15–20 minute orientation to IntegrationLab. For depth, follow the links;
for *why* things are the way they are, see [engineering-decisions.md](engineering-decisions.md).

## 1. What IntegrationLab does

IntegrationLab keeps customer records in sync between a tenant's CRM
(Salesforce-style) and the tenant's internal customer management system (CMS).
It's multi-tenant (two demo tenants: **meridian** and **harbor**), each with
its own CRM org, CMS, credentials and field mapping.

```
 CRM ──signed webhook──▶ API (verify, persist) ──▶ PostgreSQL inbox ──▶ Worker ──▶ Sync service ──▶ CMS
                          │                                              │            ├─ CRM read (OAuth)
 Operator ──OIDC token──▶ │ /sync /webhooks/events /admin /ops           │            ├─ field mapping
                          │                                              │            └─ CRM-owned fields only
                          └──────────────── sync history, attempts, audit log ◀───────┘
```

Two rules shape most of the code:
- **Tenant identity comes only from verified credentials:** a JWT `tenant_id` claim for users, or the tenant's webhook HMAC secret for the CRM.
- **The CRM owns account data, and the CMS owns onboarding status and risk classification.** Sync never writes CMS-owned fields.

## 2. Run it locally

```bash
cp .env.example .env              # set POSTGRES_PASSWORD (openssl rand -hex 24)
docker compose up -d --build --wait
TOKEN=$(curl -s localhost:8090/oauth/token -d grant_type=password -d client_id=integrationlab-cli \
  -d username=meridian-operator -d password=dev-only-password | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -H "Authorization: Bearer $TOKEN" localhost:8000/me
```

Services (all on 127.0.0.1): API 8000, mock IdP 8090, Meridian CRM/CMS 8001/8002,
Harbor CRM/CMS 8011/8012, PostgreSQL 5434, plus two `worker` replicas. The
README has a full walkthrough (CRM change → webhook → worker → CMS).

## 3. Major components

| Package | What it's responsible for |
|---|---|
| `app/main.py` | Builds the FastAPI app, includes routers, health endpoints |
| `app/wiring.py` | **Constructs every long-lived object** (engine, repositories, sync service, token verifier, worker). Plain Python, no FastAPI |
| `app/api/` | HTTP routes: `sync.py`, `webhooks.py` (ingest, events, replay), `admin.py`, `ops.py`. `dependencies.py` holds **every** FastAPI dependency: `get_principal`, `require(permission, audit_action=...)`, and providers that hand out objects from `wiring.py` |
| `app/auth/` | `tokens.py` verifies JWTs → `Principal`. `permissions.py` maps roles → permissions |
| `app/webhooks/` | `signing.py` (HMAC scheme), `ingest.py` (authenticate, validate, persist) |
| `app/worker/` | `runner.py` (claim → sync → record outcome), `retry_policy.py` (retry, fail, dead-letter), `__main__.py` (CLI) |
| `app/services/` | `account_sync.py` (**the sync itself**), `locks.py` (per-account advisory lock), `tenant_context.py` (per-tenant clients and mappings), `secrets.py` (secret references) |
| `app/integrations/` | `crm_client.py`, `internal_client.py`, `errors.py`, `retry.py` (in-client HTTP retries) |
| `app/mapping/` | `mapper.py` (mapping config schema + `FieldMapper`), `transforms.py` (allowed transforms) |
| `app/repositories/` | All SQL: `webhook_events.py` (inbox lifecycle: ingest, claim, complete, replay, attempts), `operations.py` (queue metrics, worker heartbeats), `sync_history.py`, `tenants.py`, `audit.py` |
| `app/db/` | `engine.py`, `models.py` (all tables), `errors.py` (`RepositoryError`, `RepositoryUnavailableError`) |
| `app/schemas/` | `internal_customer.py` (destination contract + **field ownership**), `sync.py` (`SyncRecord`) |
| `app/core/config.py` | All settings from env vars, plus production safety checks |
| `app/cli.py`, `retention.py`, `seed.py` | `seed-dev` and `retention` commands |
| `mock_crm/`, `mock_internal/`, `mock_idp/` | Simulated external systems (dev/test only) |
| `migrations/` | Alembic, forward-only (`0001`–`0005`) |

## 4. Where to find things

| I want to… | Look at |
|---|---|
| see what a request needs to be authorized | the route's `Depends(require(Permission.X))` (`app/api/dependencies.py`). Role table in `app/auth/permissions.py` |
| see how an object is built / configured | `app/wiring.py` |
| understand token validation | `app/auth/tokens.py` `TokenVerifier.verify` |
| see which fields sync may write | `app/schemas/internal_customer.py` `FIELD_OWNERSHIP`, `CustomerSyncPayload` |
| follow one sync end to end | `app/services/account_sync.py` `AccountSyncService.sync_account` → `_run` → `_load` |
| see webhook verification | `app/webhooks/signing.py` `verify`, `app/webhooks/ingest.py` `WebhookIngestor.ingest` |
| see claim / retry / dead-letter SQL | `app/repositories/webhook_events.py` (`_CLAIM_SQL`, `_COMPLETE_SQL`, `_REPLAY_SQL`) |
| change retry timing | `app/worker/retry_policy.py`, env `RETRY_*`, `WORKER_MAX_ATTEMPTS` |
| see how secrets are looked up | `app/services/secrets.py` `EnvSecretResolver.resolve` |
| see a table definition | `app/db/models.py` (the migration of the same name has the DDL) |

## 5. How data moves

**Webhook → CMS (normal path)**
1. `api/webhooks.py` `receive_crm_webhook` reads the raw body (≤ 64 KiB).
2. `webhooks/ingest.py` `WebhookIngestor.ingest`:
   - looks up the tenant's webhook secret (`repositories/tenants.py`, `services/secrets.py`);
   - checks the HMAC and the 5-minute replay window (`webhooks/signing.py` `verify`);
   - validates the envelope;
   - calls `repositories/webhook_events.py` `ingest`, which does `INSERT … ON CONFLICT DO NOTHING` and **commits before the 202**.
3. `worker/runner.py` `EventWorker.run_once` → `webhook_events.claim` (`FOR UPDATE SKIP LOCKED`, sets a lease and a `claim_token`). The claim transaction commits immediately.
4. `EventWorker._process` → `_run_sync` → `services/account_sync.py` `sync_account`:
   - takes the per-account advisory lock (`services/locks.py`);
   - loads the tenant's mapping and clients (`services/tenant_context.py`);
   - reads the CRM (`integrations/crm_client.py`) and maps the record (`mapping/mapper.py`);
   - checks lock ownership and the processing deadline;
   - writes **only changed CRM-owned fields** to the CMS (`integrations/internal_client.py`);
   - saves a `SyncRecord` (`repositories/sync_history.py`).
5. `EventWorker._decide` (`worker/retry_policy.py`) → `webhook_events.complete`, which records the outcome and an attempt row, fenced by `claim_token`.

**Failure:**
- **retryable** (timeouts, 5xx, lock contention) → `pending` with backoff;
- **after `WORKER_MAX_ATTEMPTS`** → `dead_letter`;
- **permanent** (not found, mapping, auth) → `failed`.

**Recovery:** `POST /webhooks/events/{id}/replay` (operator or admin) → `webhook_events.replay` moves the event back to `pending` and writes an audit row in the same transaction.

**Manual sync:** `POST /sync/accounts/{id}` (`api/sync.py`) calls the same `sync_account`.

## 6. Adding an API endpoint

1. Put the route in the matching `app/api/<area>.py`. Protect it with
   `principal: Annotated[Principal, Depends(require(Permission.X))]`, and use
   `principal.tenant_id`, **never** a tenant from the request.
2. Keep the route thin: call a service or repository method that takes `tenant_id` first.
3. Sensitive action? Use `require(Permission.X, audit_action="area.action")` (denials are audited) and record an `AuditEntry` for the outcome (see `replay_event`).
   New service or repository? Construct it in `app/wiring.py` and expose it through one provider in `app/api/dependencies.py`.
4. Tests: add a row to the permission matrix in `tests/test_api_security.py`, plus a cross-tenant test.
   Use the `auth` fixture (`tests/conftest.py`) for real signed tokens. Override each dependency once
   (`app.dependency_overrides[dependencies.get_x]`); **any provider you don't override reaches the real
   `wiring`, i.e. the database in `.env`**.
5. Document it in [api-contracts.md](api-contracts.md).

## 7. Adding or changing a CRM field mapping

- **Per tenant, no code:** an admin publishes a new version with `PUT /admin/mapping`.
  Seed files live in `app/mapping/configs/<tenant>.json`.
- **New destination field:** add it to `FIELD_OWNERSHIP` *and* `CustomerSyncPayload` in
  `app/schemas/internal_customer.py`, and to the mock CMS models in `mock_internal/main.py`.
  An import-time assert keeps the two in sync.
- **New transform:** add it to `REGISTRY` in `app/mapping/transforms.py`, with an argument model.
  Configs can only name registered transforms; nothing is ever evaluated.
- Tests: `tests/test_mapping.py`.

## 8. Investigating a failed webhook

1. `GET /webhooks/events?status=failed` (or `dead_letter`): find the event.
2. `GET /webhooks/events/{id}/attempts`: the error per attempt and the next retry time.
3. `GET /sync/history/{sync_record_id}`: the sync's stage, error code and changed fields.
4. `docker compose logs worker | grep <event id>`.
5. Nothing arriving at all? Check the CRM's delivery log (`GET localhost:8001/_admin/webhooks`, dev only). A 401 means a signature or secret problem; see [troubleshooting.md](troubleshooting.md).
6. Fixed the cause? `POST /webhooks/events/{id}/replay`.

## 9. Running tests

```bash
docker compose up -d --wait db
pytest -m "not postgres"        # ~360 fast unit tests, no database
REQUIRE_POSTGRES=1 pytest       # everything (~525); fails instead of skipping if Postgres is down
```

| Location | Contents |
|---|---|
| `tests/test_*.py` | unit tests: in-process mocks and test fakes (`tests/support/fakes.py`); no database |
| `tests/integration/test_pg_*.py` | real PostgreSQL via `TEST_DATABASE_URL` only (must be `*_test` and not the dev database; the dev `DATABASE_URL` is hidden from the test process) |
| `tests/conftest.py`, `tests/integration/conftest.py` | **fixtures only** |
| `tests/support/` | shared helpers: `data` (constants), `clients`, `identity` (test IdP), `tenants` (`World`: two in-process tenants), `db`, `servers`, `webhooks`, `mappings`, `concurrency` (bounded waits: use these instead of bare `join()`/`wait()`/`pool.apply`) |
| `tests/integration/_workers.py` | functions run in separate OS processes (concurrency tests) |

## 10. Key decisions (see engineering-decisions.md)

- **Inbox pattern** (ED-026): acknowledge webhooks only after COMMIT; process in workers.
- **Leases and claim tokens** (ED-029): `SKIP LOCKED` claims, crash recovery, stale workers fenced.
- **Session-level advisory lock** (ED-025): per (tenant, account); no transaction open during HTTP calls; verified before writes.
- **Retries** (ED-031/032): equal-jitter backoff, bounded attempts, then dead-letter.
- **Tenant only from verified credentials** (ED-021, ED-028).
- **Secret references, namespaced per tenant** (ED-022).
- **Field ownership lives in the destination contract** (ED-006).
- **Append-only audit log** (ED-035).

## Read these first (in order)

1. `README.md`
2. `docker-compose.yml`
3. `app/main.py`
4. `app/api/webhooks.py`
5. `app/webhooks/ingest.py`
6. `app/repositories/webhook_events.py` (module docstring + SQL)
7. `app/worker/runner.py`
8. `app/worker/retry_policy.py`
9. `app/services/account_sync.py`
10. `app/schemas/internal_customer.py`
11. `app/mapping/mapper.py`
12. `app/services/locks.py` (docstring)
13. `app/services/tenant_context.py`
14. `app/wiring.py`, `app/api/dependencies.py`, `app/auth/tokens.py`
15. `app/db/models.py`
