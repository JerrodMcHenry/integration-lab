# Troubleshooting

## Local environment

### `docker compose up` hangs; container stays in `Created`
Seen during M4 development: Docker Desktop accepted API calls but couldn't
start *any* new container (even `docker run --rm postgres:18.6 echo`). It
recovered on its own after a few minutes. If it persists, restart Docker
Desktop. Note that this also briefly stops containers from other projects.
The compose file deliberately uses no host bind mounts, because Docker Desktop
on macOS can also block on folder-access permission prompts for `~/Documents`.

### Port already in use
Postgres is published on `127.0.0.1:${POSTGRES_HOST_PORT:-5434}`, because 5432
and 5433 are commonly taken by other local instances. Change
`POSTGRES_HOST_PORT` in `.env` (and the port in `DATABASE_URL` /
`TEST_DATABASE_URL`).

### `required variable POSTGRES_PASSWORD is missing a value`
Create `.env` from `.env.example` and set a password: `openssl rand -hex 24`.

### Integration tests are all `SKIPPED: PostgreSQL unavailable`
The DB isn't running or `TEST_DATABASE_URL` is wrong. Start it with
`docker compose up -d --wait db`. To make this a failure instead of a skip
(CI): `REQUIRE_POSTGRES=1 pytest`.

### `Refusing to run: test database name ... must end in '_test'`
Intentional. The integration fixtures truncate tables. Point
`TEST_DATABASE_URL` at `integrationlab_test`.

## Authentication

| Symptom | Cause / action |
|---|---|
| 401 `token expired` | mock IdP tokens last 15 min. Request a new one |
| 401 `token signed with an unknown key` | the mock IdP restarted (new in-memory keys) or rotated keys less than 30 s ago. Get a new token / retry after 30 s |
| 401 `token issuer invalid` | `OIDC_ISSUER` must exactly equal the token's `iss` (compose: `http://localhost:8090`, because tokens are requested from the host) |
| 401 `token is not an access token` | the token's `typ` header isn't `at+jwt` (e.g. an ID token was sent) |
| 403 `missing permission …` | the role lacks the permission. See the matrix in security.md |
| 403 `tenant is not provisioned` / `suspended` | no active `tenants` row for the token's `tenant_id`. Run `python -m app.cli seed-dev` (dev) |
| 503 `identity provider unavailable` | API can't fetch JWKS. Check `OIDC_JWKS_URL` / that mock-idp is running |
| 500 `tenant_connection_invalid` | tenant has no `tenant_integrations` row, or a referenced `IL_<TENANT>__…` env var is missing |

## Sync results

| Symptom | Meaning | Action |
|---|---|---|
| 409, `acquire_lock` / `sync_lock_timeout` | another worker held the same account's lock longer than `SYNC_LOCK_TIMEOUT_SECONDS` | retry. Check for slow upstreams |
| 503, `acquire_lock` / `database_unavailable` | lock DB unreachable. Sync did **not** run | check `/health/ready`, DB container |
| 503 with `X-Sync-History-Persisted: false` | the sync ran (see `status` in the body) but its record wasn't saved | the record is in the API log under `sync.history_write_failed`. Fix the DB |
| 502 `destination_unavailable` / `crm_unavailable` | upstream outage after bounded retries | retry later (`retryable: true`) |
| 422 `mapping_failed` | CRM record failed validation. `error.issues` lists fields | fix the data in the CRM |
| 422 `mapping_config_invalid` | tenant mapping file invalid or missing | fix `app/mapping/configs/<tenant>.json` and restart (mappings are cached) |
| 500 `unexpected_error` | bug | stack trace in logs under `sync.unexpected_error` |

## Inspecting state

```bash
# History (most recent first)
docker compose exec db psql -U integrationlab -d integrationlab \
  -c "select seq, crm_account_id, status, stage, error_code, retryable, finished_at from sync_records order by seq desc limit 20"

# Who holds sync locks right now
docker compose exec db psql -U integrationlab -d integrationlab \
  -c "select l.pid, a.application_name, a.state, now() - a.xact_start as held_for
      from pg_locks l join pg_stat_activity a using (pid) where l.locktype = 'advisory' and l.granted"

# Applied migration
docker compose exec db psql -U integrationlab -d integrationlab -c "select * from alembic_version"
```


## Webhooks and worker

| Symptom | Cause / action |
|---|---|
| 401 `signature verification failed` | wrong secret, body changed after signing (sign the exact bytes you send), wrong tenant in the URL, or `IL_<TENANT>__WEBHOOK_SECRET` not set on the API (logged as `webhook.secret_unavailable`) |
| 401 `signature timestamp outside tolerance` | sender clock skew or a delayed/replayed request. Check NTP and `WEBHOOK_TOLERANCE_SECONDS` |
| 409 conflict | the sender reused an `event_id` for different content |
| 503 from the webhook endpoint | DB unreachable. The event was **not** stored; the sender must retry |
| events stay `pending` | no worker running (`docker compose ps worker`), or the worker can't reach the DB (logs `worker.claim_failed`, backing off) |
| events stuck `processing` | a worker died mid-event. They're reclaimed automatically when `lease_expires_at` passes (default ≈ 214 s) |
| `failed / worker_attempts_exhausted` | the event was claimed `WORKER_MAX_ATTEMPTS` times without completing (repeated crashes). Investigate worker logs. Replay arrives in M7 |
| `failed / history_not_persisted` | sync ran but its record couldn't be saved (DB issue). Retryable |
| uneven work across workers | one worker claimed a whole batch. Lower `WORKER_BATCH_SIZE` |

```bash
# Queue overview
docker compose exec db psql -U integrationlab -d integrationlab -c \
  "select tenant_id, status, count(*), max(attempts) from webhook_events group by 1,2 order by 1,2"
# In-flight leases
docker compose exec db psql -U integrationlab -d integrationlab -c \
  "select event_id, claimed_by, attempts, lease_expires_at from webhook_events where status='processing'"
# Recent failures
docker compose exec db psql -U integrationlab -d integrationlab -c \
  "select event_id, crm_account_id, last_error_code, last_error_retryable from webhook_events where status='failed' order by received_at desc limit 20"
docker compose logs -f worker
```
