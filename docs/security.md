# Security

This document lists controls that are **implemented and tested**, followed by
known gaps. It is not a claim of production readiness.

## Implemented

| Control | Where | Test evidence |
|---|---|---|
| OAuth 2.0 client credentials to the CRM; token cached, refreshed on 401 | `crm_client.py` | `test_crm_client.py`, `test_crm_contract.py` |
| CRM tokens expire server-side (mock enforces TTL, expired tokens purged) | `mock_crm/main.py` | `test_mock_rejects_expired_token`, `test_expired_tokens_are_purged` |
| Constant-time credential comparison in mocks | `mock_crm`, `mock_internal` | — |
| Production refuses dev-default secrets and non-HTTPS upstreams | `app/core/config.py` | `test_production_safety.py` |
| Mock `/_admin` routes never mounted in production; forcing them raises | `mock_common/guard.py` | `test_production_safety.py` |
| Mapping configs cannot execute code (whitelisted transforms, strict schema, no eval) | `app/mapping/` | `test_invalid_mapping_configs_are_rejected` |
| Tenant id validated before file access (path traversal) | `MappingRegistry` | `test_registry_rejects_path_traversal_and_missing_tenants` |
| Destination-owned fields cannot be written by sync | `account_sync.py`, `internal_customer.py` | `test_destination_owned_fields_preserved_across_sync` |
| Account payloads and tokens excluded from logs, errors and sync history | clients, mapper, sync service | `test_account_payload_not_logged`, `test_mapping_errors_never_echo_source_values`, `test_sync_logs_and_history_exclude_account_data` |
| Path parameters URL-encoded in outbound calls | both clients | `test_account_id_is_url_encoded` |
| Sync history reads require a tenant id | `sync_history.py` | `test_history_is_tenant_scoped` |
| Tenant is never taken from caller input | `api/deps.py` | `test_tenant_cannot_be_chosen_by_caller` |
| PostgreSQL published on 127.0.0.1 only. No credentials in the repo (compose requires `POSTGRES_PASSWORD` from gitignored `.env`) | `docker-compose.yml` | manual: LAN IP port 5434 closed |
| No account payload columns in the database | `app/db/models.py` | `test_no_account_payload_columns`, live check: 0 rows contain account names |
| SQL parameters never logged (`hide_parameters=True`) | `app/db/engine.py` | — |
| DB constraints enforce tenant id format and failure/success consistency | migration `0001` | `test_check_constraints_reject_invalid_rows` |
| Destructive downgrade requires explicit opt-in | migration `0001` | `test_destructive_downgrade_refused_without_explicit_opt_in` |
| Integration tests can't touch non-test databases | `tests/integration/conftest.py` | name must end in `_test` (verified to abort) |
| App container runs as a non-root user | `Dockerfile` | — |

## Known gaps (do not deploy)

- **IntegrationLab's API is unauthenticated.** `/sync/*` can be called by
  anyone who can reach the port. OIDC + RBAC arrive in M5.
- **Single tenant.** `get_current_tenant()` returns `DEFAULT_TENANT_ID`.
- **Secrets come from env vars.** There's no secret manager integration yet.
- **Internal CMS auth is a static API key.** It's realistic for many internal
  systems, but there's no rotation story.
- **Database connection is unencrypted** (local Docker network). Production
  would need `sslmode=verify-full` and a least-privilege DB role. The app
  currently uses the database owner role for both migrations and runtime.
- **Sync history is not tamper-evident** and has no retention policy yet.
- **Mocks are not hardened.** No rate limiting, and in-memory idempotency
  and token stores grow until restart (tokens are purged on issue).
