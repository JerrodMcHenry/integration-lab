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
