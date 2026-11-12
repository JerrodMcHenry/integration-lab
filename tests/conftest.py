"""Shared fixtures: both mock services run in-process, wired to real clients."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import CRMSettings, InternalSystemSettings
from app.integrations.crm_client import CRMClient
from app.integrations.internal_client import InternalSystemClient
from app.mapping.mapper import MappingRegistry
from app.repositories.sync_history import InMemorySyncHistoryRepository
from app.services.account_sync import AccountSyncService
from mock_crm import main as mock_crm
from mock_internal import main as mock_internal

CRM_BASE = "http://crm.test"
CMS_BASE = "http://cms.test"
CMS_KEY = "test-internal-key"
MAPPING_DIR = Path(__file__).resolve().parent.parent / "app" / "mapping" / "configs"


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def crm_app(clock):
    app = mock_crm.create_app(app_env="test", clock=clock)
    with TestClient(app, base_url=CRM_BASE) as tc:
        yield tc


@pytest.fixture
def cms_app():
    app = mock_internal.create_app(app_env="test", api_key=CMS_KEY)
    with TestClient(app, base_url=CMS_BASE) as tc:
        yield tc


def crm_settings(**overrides) -> CRMSettings:
    values = dict(
        base_url=CRM_BASE, token_url=f"{CRM_BASE}/oauth/token",
        client_id=mock_crm.CLIENT_ID, client_secret=mock_crm.CLIENT_SECRET,
        timeout_seconds=2.0, max_retries=2, backoff_base_seconds=0.0, backoff_max_seconds=0.0,
    )
    values.update(overrides)
    return CRMSettings(**values)


def cms_settings(**overrides) -> InternalSystemSettings:
    values = dict(base_url=CMS_BASE, api_key=CMS_KEY, timeout_seconds=2.0, max_retries=2,
                  backoff_base_seconds=0.0, backoff_max_seconds=0.0)
    values.update(overrides)
    return InternalSystemSettings(**values)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def crm_client(crm_app, sleeps) -> CRMClient:
    return CRMClient(crm_settings(), http_client=crm_app, sleep=sleeps.append)


@pytest.fixture
def cms_client(cms_app, sleeps) -> InternalSystemClient:
    return InternalSystemClient(cms_settings(), http_client=cms_app, sleep=sleeps.append)


@pytest.fixture
def history() -> InMemorySyncHistoryRepository:
    return InMemorySyncHistoryRepository()


@pytest.fixture
def sync_service(crm_client, cms_client, history) -> AccountSyncService:
    return AccountSyncService(crm=crm_client, destination=cms_client,
                              mappings=MappingRegistry(MAPPING_DIR), history=history)


def crm_token(crm_app: TestClient) -> str:
    r = crm_app.post("/oauth/token", data={"grant_type": "client_credentials"},
                     auth=(mock_crm.CLIENT_ID, mock_crm.CLIENT_SECRET))
    return r.json()["access_token"]


def crm_patch(crm_app: TestClient, account_id: str, **fields) -> dict:
    r = crm_app.patch(f"/crm/accounts/{account_id}", json=fields,
                      headers={"Authorization": f"Bearer {crm_token(crm_app)}"})
    assert r.status_code == 200, r.text
    return r.json()


def cms_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {CMS_KEY}"}


# --------------------------------------------------------------------------- #
# Authentication (Milestone 5)
# --------------------------------------------------------------------------- #
from app.api import deps as api_deps  # noqa: E402
from app.auth import dependencies as auth_deps  # noqa: E402
from tests.auth_helpers import TestIdP, test_tenants  # noqa: E402


@pytest.fixture(scope="session")
def idp() -> TestIdP:
    return TestIdP()


@pytest.fixture
def tenant_repo():
    return test_tenants()


@pytest.fixture
def audit_repo():
    from app.repositories.audit import InMemoryAuditRepository
    return InMemoryAuditRepository()


@pytest.fixture
def auth(idp, tenant_repo, audit_repo):
    """Install the test IdP's verifier, the test tenant directory and an in-memory audit log."""
    from app.main import app

    verifier = idp.verifier()
    app.dependency_overrides[auth_deps.get_token_verifier] = lambda: verifier
    app.dependency_overrides[auth_deps.get_tenant_repository] = lambda: tenant_repo
    app.dependency_overrides[api_deps.get_tenant_repository] = lambda: tenant_repo
    app.dependency_overrides[auth_deps.get_audit_repository] = lambda: audit_repo
    app.dependency_overrides[api_deps.get_audit_repository] = lambda: audit_repo
    yield idp
    for dep in (auth_deps.get_token_verifier, auth_deps.get_tenant_repository,
                api_deps.get_tenant_repository, auth_deps.get_audit_repository, api_deps.get_audit_repository):
        app.dependency_overrides.pop(dep, None)
