"""Development-only endpoints and dev secrets must not survive into production."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import ConfigurationError, load_settings
from mock_common.guard import UnsafeAdminConfiguration, admin_routes_enabled
from mock_crm import main as mock_crm
from mock_internal import main as mock_internal


@pytest.mark.parametrize(
    "env, flag, expected",
    [
        ("development", None, True),
        ("test", None, True),
        ("development", "false", False),
        ("production", None, False),
        ("PRODUCTION", None, False),
        ("staging", None, False),
        ("production", "false", False),
        ("qa", None, False),            # unknown env: fail closed
        ("qa", "true", True),           # unknown env: explicit opt-in allowed
    ],
)
def test_admin_guard_truth_table(env, flag, expected):
    assert admin_routes_enabled(env, flag) is expected


@pytest.mark.parametrize("env", ["production", "prod", "staging", " Production "])
def test_admin_enabled_in_production_refuses_to_start(env):
    with pytest.raises(UnsafeAdminConfiguration):
        admin_routes_enabled(env, "true")


def test_unrecognised_flag_value_refuses_to_start():
    with pytest.raises(UnsafeAdminConfiguration):
        admin_routes_enabled("development", "maybe")


@pytest.mark.parametrize("module", [mock_crm, mock_internal])
def test_production_mock_has_no_admin_routes(module):
    app = module.create_app(app_env="production")
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any(p.startswith("/_admin") for p in paths)
    with TestClient(app) as tc:
        assert tc.put("/_admin/faults", json={"mode": "error", "remaining": 5}).status_code == 404
        assert tc.post("/_admin/reset").status_code == 404
        assert "/_admin/faults" not in tc.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("module", [mock_crm, mock_internal])
def test_forcing_admin_in_production_raises(module):
    with pytest.raises(UnsafeAdminConfiguration):
        module.create_app(app_env="production", admin_enabled=True)


def admin_reachable(app, method="PUT", path="/_admin/faults") -> bool:
    """Probe over HTTP. Route-table inspection is unreliable: FastAPI 0.16x registers
    included routers as nested entries whose path is '' (found in M5)."""
    with TestClient(app) as tc:
        return tc.request(method, path, json={"mode": "none"}).status_code != 404


@pytest.mark.parametrize("module", [mock_crm, mock_internal])
def test_production_env_var_disables_admin(module, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("MOCK_ADMIN_ENABLED", raising=False)
    assert admin_reachable(module.create_app())  # control: the probe really detects the routes
    monkeypatch.setenv("APP_ENV", "production")
    assert not admin_reachable(module.create_app())


# --- IntegrationLab settings ---------------------------------------------- #
PROD_OK = {
    "APP_ENV": "production",
    "DATABASE_URL": "postgresql+psycopg://app@db.example.com/integrationlab",
    "OIDC_ISSUER": "https://login.example.com/realms/integrationlab",
}


def _apply(monkeypatch, env):
    for k in ("OIDC_JWKS_URL", *PROD_OK):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_production_settings_with_real_values_load(monkeypatch):
    _apply(monkeypatch, PROD_OK)
    assert load_settings().is_production


def test_obsolete_global_crm_cms_variables_are_ignored(monkeypatch):
    """Since M5 every connection is per tenant; global CRM/CMS credentials no longer exist (Phase 3)."""
    _apply(monkeypatch, {**PROD_OK, "CRM_CLIENT_SECRET": "dev-only-secret", "INTERNAL_API_KEY": "dev-only-key",
                         "CRM_BASE_URL": "http://insecure.example.com"})
    settings = load_settings()  # dev-looking legacy values neither required nor rejected
    for name in ("base_url", "token_url", "client_id", "client_secret", "api_key"):
        assert not hasattr(settings.crm, name) and not hasattr(settings.internal, name)


@pytest.mark.parametrize(
    "override, message",
    [
        ({"OIDC_ISSUER": "http://login.example.com"}, "OIDC_ISSUER must use https"),
        ({"OIDC_ISSUER": "http://localhost:8090"}, "development mock IdP"),
        ({"OIDC_JWKS_URL": "http://login.example.com/jwks"}, "OIDC_JWKS_URL must use https"),
    ],
)
def test_production_rejects_dev_secrets_and_plain_http(monkeypatch, override, message):
    _apply(monkeypatch, {**PROD_OK, **override})
    with pytest.raises(ConfigurationError, match=message):
        load_settings()


# --- Mock IdP (Milestone 5) --------------------------------------------------- #
from mock_idp import main as mock_idp  # noqa: E402


@pytest.mark.parametrize("env", ["production", "prod", "staging", " Staging "])
def test_mock_idp_refuses_to_start_in_production_like_envs(env):
    with pytest.raises(mock_idp.UnsafeIdPConfiguration):
        mock_idp.create_app(app_env=env)


def test_mock_idp_admin_routes_only_in_dev():
    assert admin_reachable(mock_idp.create_app(app_env="development"), "POST", "/_admin/rotate-keys")
    # Unknown env: fail closed.
    assert not admin_reachable(mock_idp.create_app(app_env="qa"), "POST", "/_admin/rotate-keys")


def test_mock_idp_rejects_bad_credentials_and_grants():
    with TestClient(mock_idp.create_app(app_env="test")) as tc:
        ok = {"grant_type": "password", "client_id": "integrationlab-cli", "username": "meridian-admin",
              "password": mock_idp.DEV_PASSWORD}
        assert tc.post("/oauth/token", data=ok).status_code == 200
        assert tc.post("/oauth/token", data={**ok, "password": "wrong"}).status_code == 400
        assert tc.post("/oauth/token", data={**ok, "username": "nobody"}).status_code == 400
        assert tc.post("/oauth/token", data={**ok, "client_id": "evil"}).status_code == 401
        assert tc.post("/oauth/token", data={"grant_type": "implicit"}).status_code == 400
        assert tc.post("/oauth/token", data={"grant_type": "client_credentials"},
                       auth=("meridian-automation", "wrong")).status_code == 401
