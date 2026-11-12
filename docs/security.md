# Security

This document describes controls that are **implemented and tested**,
followed by known gaps. It is not a claim of production readiness.

## Authentication flow (OIDC / OAuth 2.0)

```
 user / service                 Identity provider                    IntegrationLab API
 ──────────────                 ─────────────────                    ──────────────────
 1. POST /oauth/token  ───────▶  authenticates the user or client
                       ◀───────  RS256 JWT access token (RFC 9068)
                                 header: alg, kid, typ=at+jwt
                                 claims: iss, aud, sub, iat, nbf, exp, jti,
                                         client_id, tenant_id, roles
 2. Authorization: Bearer <JWT>  ─────────────────────────────────▶  3. verify (app/auth/tokens.py)
                                 4. GET /jwks (cached, 10 min) ◀────    a. alg ∈ allowlist (RS256…; never none/HS*)
                                                                        b. typ == at+jwt (not an ID token)
                                                                        c. kid → key from JWKS
                                                                        d. signature
                                                                        e. iss exact, aud contains integrationlab-api
                                                                        f. exp / nbf / iat (30 s leeway),
                                                                           lifetime ≤ 1 h, sub present
                                                                        g. tenant_id well-formed, roles is a list
                                                                     5. tenants table: tenant exists and is active
                                                                     6. role → permissions; endpoint requires one
                                                                     7. handler uses principal.tenant_id only
```

- **Tenant identity comes only from the verified `tenant_id` claim** and must
  match an active row in `tenants`. Paths, query strings, headers and bodies
  are never used for tenant identity (tested with `?tenant_id=`, `X-Tenant-ID`
  and `X-Forwarded-Tenant`).
- **JWKS:** fetched from `OIDC_JWKS_URL`, or discovered via
  `{issuer}/.well-known/openid-configuration` (the discovery `issuer` must
  match). Cached for `OIDC_JWKS_CACHE_SECONDS`. An unknown `kid` triggers a
  refetch at most once every 30 s, so a flood of bogus `kid`s can't hammer the
  IdP (tested). Key rotation works if the IdP publishes new keys before signing
  with them, which standard IdPs do.
- **Errors (RFC 6750):** 401 + `WWW-Authenticate: Bearer error="invalid_token"`
  for a missing, invalid or expired token. 403 + `error="insufficient_scope"` for
  a missing permission. 403 for an unknown or suspended tenant. 503 if the IdP's
  keys can't be fetched (never a silent 401). Tokens are never logged (tested).

### Identity provider

`mock_idp/` is a **development and test** IdP. It implements discovery, JWKS,
RS256 `at+jwt` access tokens, the `password` grant for named demo users and
`client_credentials` for a tenant-bound service account, plus dev-only key
rotation. It **refuses to start** when `APP_ENV` is production, prod or staging.
For the dashboard it implements the **authorization code flow with PKCE**
(below). It does not implement ID tokens, refresh tokens or userinfo. The API's verifier is IdP-agnostic: point `OIDC_ISSUER`,
`OIDC_AUDIENCE` (and optionally `OIDC_JWKS_URL`, `OIDC_TENANT_CLAIM`,
`OIDC_ROLES_CLAIM`) at Keycloak, Entra ID, Auth0 or Okta configured to emit
`tenant_id` and `roles` claims.

Demo users (password `dev-only-password`): `meridian-admin`,
`meridian-operator`, `meridian-viewer`, `meridian-norole`, `harbor-admin`,
`harbor-operator`.

### Dashboard sign-in (authorization code + PKCE)

The dashboard (`dashboard/`) is a **public client** (`integrationlab-dashboard`):
it has no client secret, and none appears in the browser bundle.

- **PKCE, S256 only.** The browser generates a random verifier, sends only its
  SHA-256 challenge to `/authorize`, and proves possession of the verifier at
  `/oauth/token`. `plain` is rejected.
- **Authorization codes** are random, valid for 60 s, and deleted on the first
  redemption attempt, including a failed one. A code is bound to the client,
  redirect URI and challenge it was issued for.
- **Redirect URIs** must match the allow-list exactly
  (`MOCK_IDP_DASHBOARD_REDIRECT_URIS`; default `http://localhost:5173/callback`
  and `http://localhost:8080/callback`). An invalid request shows an error page
  and is never redirected.
- **State:** the browser sends a random `state`, stores it with the verifier for
  this tab only, and rejects a callback whose `state` doesn't match. The pending
  entry is deleted before checking, so a callback can't be used twice.
- **The login page** escapes every reflected value and sends `no-store`,
  `X-Frame-Options: DENY` and a restrictive CSP.
- **The token** lives in `sessionStorage`: one tab, cleared when it closes. This
  is acceptable for the local demo only. Any 401 returns the user to sign-in.
  There are no refresh tokens; sessions last as long as the access token (15 min).
- **Same origin:** nginx (or Vite in development) serves the dashboard and
  forwards `/api` and `/idp`, so the API has no CORS configuration. The
  dashboard's pages send a CSP allowing scripts, styles and connections only
  from the same origin, plus `nosniff` and `Referrer-Policy: no-referrer`.
- **The API stays authoritative.** The dashboard hides or disables actions based
  on `GET /me` permissions as a convenience only. Every request is still
  authorized, tenant-scoped and (for replay) audited by the API.

## Webhook authentication (Milestone 6)

Inbound CRM webhooks don't use bearer tokens. Each request is authenticated
by an HMAC over its **raw body** with the tenant's webhook secret:

1. The tenant in the URL is only a hint. Its integration row supplies the
   secret reference(s) (`env:IL_<TENANT>__WEBHOOK_SECRET`, plus an optional
   previous secret for rotation), resolved through the same namespace-checked
   resolver as the CRM credentials.
2. `HMAC-SHA256(secret, "{t}.{raw body}")` is compared in constant time
   against every `v1=` value. Unknown tenant, missing secret and bad signature
   all return the identical 401 (no tenant-enumeration oracle).
3. Only after the signature verifies: `|now − t| ≤ WEBHOOK_TOLERANCE_SECONDS`
   (default 300 s). Inside the window, a replayed request has the same
   `event_id` and is deduplicated by `UNIQUE (tenant_id, source, event_id)`.
4. A `tenant_id` inside the payload is never trusted. If present and different
   from the authenticated tenant, the event is rejected.
5. Secrets and signatures are never logged or stored. `webhook_events.payload`
   holds only an allowlisted envelope (tested against DB contents and logs, plus
   a live check).

## Audit and operational controls (Milestone 7)

- **Audit log:** append-only at the database level (a trigger blocks
  UPDATE, DELETE and TRUNCATE). Records replay (success/rejected/**denied**),
  bulk replay, mapping publishes (success/rejected/denied) and retention purges.
  `details` rejects keys resembling secrets, tokens, signatures or passwords.
  Readable only by the tenant's admins (`GET /admin/audit`). Also emitted as
  JSON log lines.
- **Replay:** tenant-scoped (another tenant's event id → 404, no state change),
  only from `failed`/`dead_letter`, atomic with its audit record, at most 100 per
  bulk call.
- **Platform metrics** (`/metrics`, all tenants) use a separate static token
  (≥ 32 chars enforced in production), compared in constant time. A user JWT,
  even an admin's, is not accepted. Disabled unless configured. Tenant users get
  only their own `/ops/metrics`, with worker health as counts (no hostnames).
- **Retention:** destructive cleanup is off by default and needs both
  `RETENTION_ENABLED=true` and `--execute`. Audited.

## Permission model (RBAC)

| Permission | viewer | operator | admin | Endpoints |
|---|:-:|:-:|:-:|---|
| `tenant:read` | ✅ | ✅ | ✅ | `GET /tenant` |
| `history:read` | ✅ | ✅ | ✅ | `GET /sync/history`, `GET /sync/history/{id}`, `GET /webhooks/events[/{id}[/attempts]]`, `GET /ops/metrics` |
| `sync:run` | | ✅ | ✅ | `POST /sync/accounts/{id}`, `POST /webhooks/events/{id}/replay`, `POST /webhooks/events/replay` |
| `config:read` | | | ✅ | `GET /admin/integration`, `GET /admin/mapping`, `GET /admin/mapping/versions`, `GET /admin/audit` |
| `config:write` | | | ✅ | `PUT /admin/mapping` |
| (authenticated) | ✅ | ✅ | ✅ | `GET /me` |
| (HMAC) | | | | `POST /webhooks/crm/{tenant_id}`, authenticated by tenant webhook signature |
| (public) | | | | `GET /health`, `GET /health/ready`, `/docs` and `/openapi.json` (development only) |

- Endpoints depend on permissions, not role names (`app/auth/permissions.py`).
- Unknown roles in a token are ignored. A token with no known role is
  authenticated but has no permissions (403 everywhere except `/me`).
- All roles are tenant-scoped. There is **no cross-tenant role** in the API.
  Platform operations (tenant provisioning, integration settings) happen via
  the CLI and database, not the API.
- Tenant admins can publish mapping versions (they can't execute code or
  target destination-owned fields; see ED-007). They **cannot** change
  connection URLs (SSRF) or secret references (credential theft); those are
  operator-managed.

## Tenant isolation

| Layer | Control | Evidence |
|---|---|---|
| Identity | tenant from verified token only. Tenant must exist and be active | `test_api_security.py` |
| HTTP | no endpoint accepts a tenant id. Another tenant's record id → 404 (not 403, so no existence oracle) | `test_history_is_isolated_between_tenants` |
| Repositories | every method takes `tenant_id` first. No "list all tenants' data" method exists | `test_pg_tenants.py`, `test_pg_history.py` |
| Database | FK `tenant_id → tenants`. Mapping rows CHECK `config->>'tenant_id' = tenant_id` | `test_tenant_constraints` |
| Connections | a separate CRM/CMS client per tenant, built from that tenant's row and secrets. **OAuth tokens are never shared between tenants** | `test_crm_tokens_are_never_shared_between_tenants` |
| Secrets | DB stores references only (`env:IL_<TENANT>__<NAME>`). The resolver parses the reference and requires an exact tenant-segment match | `test_resolver_prefix_collision_between_tenant_ids_is_impossible` |
| Mappings | stored and versioned per tenant. A stored config naming another tenant is refused | `test_stored_mapping_for_wrong_tenant_is_rejected` |
| Concurrency | the advisory-lock key includes tenant id | `test_scope_is_tenant_and_account` |

**Bug found and fixed during M5:** the first secret-namespace check was a
prefix match on `IL_<TENANT>_`, which would have let tenant `a` resolve
`IL_A_B_KEY` belonging to tenant `a-b`. It was replaced by parsing with a
`__` delimiter and a strict name grammar. The regression tests fail against
the old logic (verified).

## Other implemented controls

| Control | Where | Evidence |
|---|---|---|
| OAuth 2.0 client credentials to each tenant's CRM, with a per-tenant token cache | `crm_client.py`, `tenant_context.py` | `test_crm_contract.py`, `test_tenant_isolation.py` |
| Production refuses: `dev-only*` tenant secrets, non-HTTPS tenant CRM/CMS endpoints, non-HTTPS OIDC issuer/JWKS, the mock IdP issuer, a missing `DATABASE_URL` | `config.py`, `secrets.py` | `test_production_safety.py` |
| OpenAPI and interactive docs not served in production | `app/main.py` | `test_docs_disabled_in_production` |
| Mock `/_admin` routes never mounted in production/staging; forcing them raises | `mock_common/guard.py` | HTTP-probe tests in `test_production_safety.py` |
| Mock IdP refuses to start in production/staging | `mock_idp/main.py` | `test_mock_idp_refuses_to_start_in_production_like_envs` |
| Mapping configs cannot execute code | `app/mapping/` | `test_invalid_mapping_configs_are_rejected` |
| Destination-owned fields cannot be written by sync | `account_sync.py` | `test_destination_owned_fields_preserved_across_sync` |
| Account payloads and tokens excluded from logs, errors and history | clients, mapper, service, auth | multiple tests |
| No account payload columns in the database. SQL parameters never logged | `models.py`, `engine.py` | `test_no_account_payload_columns` |
| PostgreSQL and all services on 127.0.0.1 only. No credentials in the repo | `docker-compose.yml` | manual LAN check |
| Destructive downgrades need `ALLOW_DESTRUCTIVE_DOWNGRADE=1` | migrations | `test_destructive_downgrade_refused_without_explicit_opt_in` |
| Containers run as a non-root user | `Dockerfile` | — |

## Remaining risks (do not deploy)

1. **Mock IdP only.** It has no user store, sessions, consent, MFA or refresh
   tokens, and keeps authorization codes in memory (a single process). The
   `password` grant is a dev convenience and is deprecated in OAuth 2.1. The
   dashboard keeps its token in `sessionStorage`, which is exposed to any script
   running on the page; acceptable for the local demo, but a production
   deployment should use a backend-for-frontend or in-memory tokens with a real IdP.
2. **No token revocation.** A stolen access token is valid until `exp`
   (15 min from the mock IdP, ≤ 1 h by policy). Mitigations: short lifetimes;
   suspending the tenant blocks all of its tokens immediately.
3. **Roles are trusted from the IdP.** A misconfigured IdP mapping (e.g.
   everyone gets `admin`) is not detectable by the API.
4. **Tenant-id prefix hygiene.** Tenant ids containing `--` or a trailing `-`
   can't resolve secrets (they fail closed). Provisioning should forbid them.
5. **Secrets in environment variables.** No secret manager, rotation workflow
   or audit of secret access. The resolver interface allows a vault backend.
6. **No row-level security.** Isolation is enforced in the application and
   repository layer. PostgreSQL RLS with a separate non-owner runtime role would
   add defence in depth. The app currently connects as the database owner for
   both migrations and runtime.
7. **Unencrypted DB connection** (local Docker network). Production needs
   `sslmode=verify-full`.
8. **No rate limiting** on authentication failures or sync triggering.
9. **No audit log** of administrative actions beyond `created_by` on mapping versions.
10. **Mapping-publish authority.** Tenant admins can change how CRM data maps
    to CRM-owned fields, which is within their tenant and audited by
    `created_by`, but there's no approval workflow.
11. **Lost-lock edge case** (M4): see "Concurrency lock: lost-lock mitigation"
    in [architecture.md](architecture.md). Documented, not yet implemented.
12. **Webhook endpoint is unauthenticated until the HMAC check**, so it does a
    tenant lookup and a secret resolution per request. There's no rate limiting:
    an attacker can force cheap DB reads (no writes happen without a valid signature).
13. **Shared-secret HMAC.** Anyone holding a tenant's webhook secret can submit
    events for that tenant. Events only trigger a re-fetch from the tenant's own
    CRM (payload fields aren't applied), which limits the impact to extra syncs.
14. **Replay window relies on clock sync** between CRM and API (NTP). Skew
    beyond the tolerance causes rejections (fail closed).
15. ~~No retention policy~~ Implemented in M7 (opt-in). The audit log grows
    without bound by design. Archiving needs a DBA procedure.
16. **Ownership checks narrow but don't close the stale-write window.** A lock
    or lease lost *during* an in-flight destination HTTP call isn't detected
    (see architecture.md). Destination-side conditional writes are needed for full fencing.
17. **Audit completeness:** only the listed actions are audited. Failed
    *authentication* (401) isn't written to the audit table (it's logged), to
    avoid unauthenticated callers filling it.
18. **`METRICS_TOKEN` is a long-lived shared secret** with read access to
    cross-tenant aggregate counts. It must be stored and rotated like other secrets.
