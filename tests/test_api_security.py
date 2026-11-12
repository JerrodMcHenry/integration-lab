"""API authentication, authorization and cross-tenant isolation (negative security tests)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_sync_history, get_sync_service
from app.main import app
from tests.support.identity import new_rsa_key
from tests.support.data import HARBOR_MAPPING, MAPPING, ROOT


PROTECTED = [
    ("POST", "/sync/accounts/SF-1001", None),
    ("GET", "/sync/history", None),
    ("GET", f"/sync/history/{uuid.uuid4()}", None),
    ("GET", "/me", None),
    ("GET", "/tenant", None),
    ("GET", "/admin/integration", None),
    ("GET", "/admin/mapping", None),
    ("GET", "/admin/mapping/versions", None),
    ("PUT", "/admin/mapping", MAPPING),
]


@pytest.fixture
def client(auth, sync_service, history, tenant_repo):
    # Seed mapping versions so admin read endpoints have something to return.
    tenant_repo.add_mapping_version("meridian", MAPPING, "seed")
    tenant_repo.add_mapping_version("harbor", HARBOR_MAPPING, "seed")
    app.dependency_overrides[get_sync_service] = lambda: sync_service
    app.dependency_overrides[get_sync_history] = lambda: history
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.pop(get_sync_service, None)
    app.dependency_overrides.pop(get_sync_history, None)


def call(client, method, path, body, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


# --------------------------------------------------------------------------- #
# Unauthenticated / bad tokens -> 401
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method, path, body", PROTECTED)
def test_no_token_is_401_everywhere(client, method, path, body):
    r = call(client, method, path, body)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize("method, path, body", PROTECTED)
def test_expired_token_is_401_everywhere(client, auth, method, path, body):
    r = call(client, method, path, body,
             auth.headers("admin", iat_offset=-1000, lifetime=300))
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert r.json()["detail"] == "token expired"


@pytest.mark.parametrize("method, path, body", PROTECTED)
def test_invalid_signature_is_401_everywhere(client, auth, method, path, body):
    r = call(client, method, path, body, auth.headers("admin", key=new_rsa_key()))
    assert r.status_code == 401 and r.json()["detail"] == "token signature invalid"


@pytest.mark.parametrize("headers", [
    {"Authorization": "Basic bWVyaWRpYW46cGFzcw=="},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer not-a-jwt"},
    {"Authorization": "bearer"},
    {"X-Api-Key": "anything"},
])
def test_malformed_credentials(client, headers):
    assert client.get("/sync/history", headers=headers).status_code == 401


def test_wrong_audience_and_issuer(client, auth):
    assert client.get("/me", headers=auth.headers(aud="another-api")).status_code == 401
    assert client.get("/me", headers=auth.headers(iss="https://evil.example")).status_code == 401


def test_auth_failures_do_not_leak_token_contents(client, auth, caplog):
    token_headers = auth.headers("admin", key=new_rsa_key())
    client.get("/me", headers=token_headers)
    assert token_headers["Authorization"].split()[1] not in caplog.text


# --------------------------------------------------------------------------- #
# RBAC -> 403 for insufficient permissions
# --------------------------------------------------------------------------- #
MATRIX = {
    #                                    viewer operator admin norole
    ("POST", "/sync/accounts/SF-1001"): (403, 201, 201, 403),
    ("GET", "/sync/history"):           (200, 200, 200, 403),
    ("GET", "/me"):                     (200, 200, 200, 200),
    ("GET", "/tenant"):                 (200, 200, 200, 403),
    ("GET", "/admin/integration"):      (403, 403, 200, 403),
    ("GET", "/admin/mapping"):          (403, 403, 200, 403),
    ("GET", "/admin/mapping/versions"): (403, 403, 200, 403),
    ("PUT", "/admin/mapping"):          (403, 403, 201, 403),
}
ROLES = (["viewer"], ["operator"], ["admin"], [])


@pytest.mark.parametrize("endpoint, expected", MATRIX.items(), ids=lambda x: str(x))
def test_permission_matrix(client, auth, endpoint, expected):
    method, path = endpoint
    body = MAPPING if method == "PUT" else None
    for roles, want in zip(ROLES, expected):
        r = client.request(method, path, json=body,
                           headers={"Authorization": f"Bearer {auth.token(roles=roles)}"})
        # Sync returns 201 the first time and 200 afterwards; both mean "allowed".
        got = 201 if (want == 201 and r.status_code == 200 and method == "POST") else r.status_code
        assert got == want, f"{roles} {method} {path}: {r.status_code} {r.text[:200]}"


def test_403_carries_insufficient_scope(client, auth):
    r = client.post("/sync/accounts/SF-1001", headers=auth.headers("viewer"))
    assert r.status_code == 403
    assert 'error="insufficient_scope"' in r.headers["www-authenticate"]
    assert 'scope="sync:run"' in r.headers["www-authenticate"]


def test_me_reports_roles_and_permissions(client, auth):
    body = client.get("/me", headers=auth.headers("operator", "harbor", sub="ops@harbor")).json()
    assert body == {"subject": "ops@harbor", "tenant_id": "harbor", "roles": ["operator"],
                    "permissions": ["history:read", "sync:run", "tenant:read"]}


# --------------------------------------------------------------------------- #
# Tenant provisioning state
# --------------------------------------------------------------------------- #
def test_unknown_tenant_forbidden(client, auth):
    r = client.get("/sync/history", headers=auth.headers("admin", "ghost-corp"))
    assert r.status_code == 403 and r.json()["detail"] == "tenant is not provisioned"


def test_suspended_tenant_forbidden(client, auth):
    r = client.get("/sync/history", headers=auth.headers("admin", "suspended-co"))
    assert r.status_code == 403 and r.json()["detail"] == "tenant is suspended"


# --------------------------------------------------------------------------- #
# Cross-tenant access
# --------------------------------------------------------------------------- #
def test_history_is_isolated_between_tenants(client, auth, sync_service):
    meridian_rec = client.post("/sync/accounts/SF-1001", headers=auth.headers("operator", "meridian")).json()
    harbor_rec = sync_service.sync_account("harbor", "SF-1002")

    harbor_view = client.get("/sync/history", headers=auth.headers("viewer", "harbor")).json()
    meridian_view = client.get("/sync/history", headers=auth.headers("viewer", "meridian")).json()

    assert [r["id"] for r in harbor_view] == [str(harbor_rec.id)]
    assert [r["id"] for r in meridian_view] == [meridian_rec["id"]]
    # Direct object reference to the other tenant's record: 404, not 403 (no existence oracle).
    assert client.get(f"/sync/history/{meridian_rec['id']}",
                      headers=auth.headers("admin", "harbor")).status_code == 404


def test_tenant_params_and_headers_are_ignored(client, auth):
    h = auth.headers("operator", "harbor")
    client.post("/sync/accounts/SF-1001", params={"tenant_id": "meridian"},
                headers={**h, "X-Tenant-ID": "meridian", "X-Forwarded-Tenant": "meridian"})
    meridian = client.get("/sync/history", headers=auth.headers("viewer", "meridian")).json()
    assert meridian == []


def test_integration_config_only_shows_own_tenant_and_redacts_secrets(client, auth):
    harbor = client.get("/admin/integration", headers=auth.headers("admin", "harbor")).json()
    meridian = client.get("/admin/integration", headers=auth.headers("admin", "meridian")).json()
    assert harbor["crm_base_url"] == "http://crm-harbor.test"
    assert meridian["crm_base_url"] == "http://crm-meridian.test"
    for body in (harbor, meridian):
        assert body["crm_client_secret"] == "configured" and body["cms_api_key"] == "configured"
        assert "env:" not in json.dumps(body) and "IL_" not in json.dumps(body)


def test_mapping_reads_are_tenant_scoped(client, auth):
    h = client.get("/admin/mapping", headers=auth.headers("admin", "harbor")).json()
    m = client.get("/admin/mapping", headers=auth.headers("admin", "meridian")).json()
    assert h["config"]["tenant_id"] == "harbor" and m["config"]["tenant_id"] == "meridian"


def test_admin_cannot_publish_mapping_for_another_tenant(client, auth, tenant_repo):
    before = tenant_repo.get_active_mapping("meridian").version
    r = client.put("/admin/mapping", json={**MAPPING, "tenant_id": "meridian"},
                   headers=auth.headers("admin", "harbor"))
    assert r.status_code == 422
    assert tenant_repo.get_active_mapping("meridian").version == before


def test_publishing_mapping_affects_only_own_tenant(client, auth, tenant_repo):
    harbor_before = tenant_repo.get_active_mapping("harbor").version
    r = client.put("/admin/mapping", json=MAPPING, headers=auth.headers("admin", "meridian"))
    assert r.status_code == 201
    assert r.json()["version"] == 2 and r.json()["created_by"] == "user-meridian"
    assert tenant_repo.get_active_mapping("harbor").version == harbor_before


def test_invalid_mapping_rejected_with_problems(client, auth):
    bad = {**MAPPING, "fields": MAPPING["fields"] + [{"source": "Rating", "target": "risk_classification"}]}
    r = client.put("/admin/mapping", json=bad, headers=auth.headers("admin"))
    assert r.status_code == 422
    assert any("internal-owned" in p for p in r.json()["detail"]["problems"])


# --------------------------------------------------------------------------- #
# Public endpoints and production hardening
# --------------------------------------------------------------------------- #
def test_health_endpoints_are_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code in (200, 503)


def test_idp_outage_returns_503_not_401(auth, tenant_repo):
    from app.api import dependencies as deps
    from app.auth.tokens import IdentityProviderUnavailable, JWKSCache, TokenVerifier

    def down():
        raise IdentityProviderUnavailable("down")

    app.dependency_overrides[deps.get_token_verifier] = lambda: TokenVerifier(
        auth.settings(), JWKSCache(down, ttl_seconds=600))
    r = TestClient(app).get("/me", headers=auth.headers())
    assert r.status_code == 503 and r.json()["detail"] == "identity provider unavailable"


def test_docs_disabled_in_production():
    env = {**os.environ, "APP_ENV": "production", "DATABASE_URL": "postgresql+psycopg://u@db.example.com/d",
           "OIDC_ISSUER": "https://login.example.com", "OIDC_JWKS_URL": ""}
    code = ("from app.main import app; "
            "print(app.openapi_url, app.docs_url, app.redoc_url)")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "None None None"


def test_legacy_accounts_endpoint_removed(client, auth):
    """Phase 3: the M1 echo endpoint (validated and returned the body, stored nothing) was removed."""
    r = client.post("/accounts", json={"account_id": "x", "account_name": "y", "relationship_manager": "z"},
                    headers=auth.headers("admin"))
    assert r.status_code == 404
    assert "/accounts" not in client.get("/openapi.json").json()["paths"]
