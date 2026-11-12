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


@pytest.mark.parametrize("module", [mock_crm, mock_internal])
def test_production_env_var_disables_admin(module, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("MOCK_ADMIN_ENABLED", raising=False)
    app = module.create_app()
    assert not any(getattr(r, "path", "").startswith("/_admin") for r in app.routes)


# --- IntegrationLab settings ---------------------------------------------- #
PROD_OK = {
    "APP_ENV": "production",
    "CRM_BASE_URL": "https://crm.example.com",
    "CRM_CLIENT_SECRET": "a-real-secret-from-a-vault",
    "INTERNAL_API_BASE_URL": "https://cms.example.com",
    "INTERNAL_API_KEY": "a-real-key-from-a-vault",
}


def _apply(monkeypatch, env):
    for k in ("CRM_TOKEN_URL", *PROD_OK):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_production_settings_with_real_values_load(monkeypatch):
    _apply(monkeypatch, PROD_OK)
    assert load_settings().is_production


@pytest.mark.parametrize(
    "override, message",
    [
        ({"CRM_CLIENT_SECRET": "dev-only-secret"}, "CRM_CLIENT_SECRET"),
        ({"INTERNAL_API_KEY": "dev-only-internal-key"}, "INTERNAL_API_KEY"),
        ({"CRM_BASE_URL": "http://crm.example.com"}, "https"),
    ],
)
def test_production_rejects_dev_secrets_and_plain_http(monkeypatch, override, message):
    _apply(monkeypatch, {**PROD_OK, **override})
    with pytest.raises(ConfigurationError, match=message):
        load_settings()
