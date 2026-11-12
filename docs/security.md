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

## Known gaps (do not deploy)

- **IntegrationLab's API is unauthenticated.** `/sync/*` can be called by
  anyone who can reach the port. OIDC + RBAC arrive in M5.
- **Single tenant.** `get_current_tenant()` returns `DEFAULT_TENANT_ID`.
- **Secrets come from env vars.** There's no secret manager integration yet.
- **Internal CMS auth is a static API key.** It's realistic for many internal
  systems, but there's no rotation story.
- **Sync history is in memory.** No durable audit trail until M4.
- **Mocks are not hardened.** No rate limiting, and in-memory idempotency
  and token stores grow until restart (tokens are purged on issue).
