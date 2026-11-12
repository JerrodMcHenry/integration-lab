"""Shared fixtures: both mock services run in-process, wired to real clients."""

from __future__ import annotations

# Must run before anything imports `app`: the test process never sees the development DATABASE_URL.
from tests.support.db import isolate_database_environment

isolate_database_environment()

from pathlib import Path  # noqa: E402

import pytest
from fastapi.testclient import TestClient

from app.integrations.crm_client import CRMClient
from app.integrations.internal_client import InternalSystemClient
from tests.support.mappings import MappingRegistry
from tests.support.fakes import InMemorySyncHistoryRepository
from app.services.account_sync import AccountSyncService
from mock_crm import main as mock_crm
from mock_internal import main as mock_internal

CRM_BASE = "http://crm.test"
CMS_BASE = "http://cms.test"
CMS_KEY = "test-internal-key"
MAPPING_DIR = Path(__file__).resolve().parent.parent / "app" / "mapping" / "configs"


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
    return AccountSyncService(connections=StaticConnections(crm_client, cms_client),
                              mappings=MappingRegistry(MAPPING_DIR), history=history, locks=InProcessSyncLocks())


# --------------------------------------------------------------------------- #
# Authentication (Milestone 5)
# --------------------------------------------------------------------------- #
from app.api import dependencies as deps  # noqa: E402
from tests.support.identity import TestIdP
from tests.support.tenants import in_memory_tenants
from tests.support.clients import FakeClock, cms_settings, crm_settings
from tests.support.data import CMS_BASE, CMS_KEY, CRM_BASE, MAPPING_DIR
from tests.support.fakes import InProcessSyncLocks, StaticConnections


@pytest.fixture(scope="session")
def idp() -> TestIdP:
    return TestIdP()


@pytest.fixture
def tenant_repo():
    return in_memory_tenants()


@pytest.fixture
def audit_repo():
    from tests.support.fakes import InMemoryAuditRepository
    return InMemoryAuditRepository()


@pytest.fixture
def auth(idp, tenant_repo, audit_repo):
    """Install the test IdP's verifier, the test tenant directory and an in-memory audit log."""
    from app.main import app

    verifier = idp.verifier()
    app.dependency_overrides[deps.get_token_verifier] = lambda: verifier
    app.dependency_overrides[deps.get_tenant_repository] = lambda: tenant_repo
    app.dependency_overrides[deps.get_audit_repository] = lambda: audit_repo
    yield idp
    for dep in (deps.get_token_verifier, deps.get_tenant_repository, deps.get_audit_repository):
        app.dependency_overrides.pop(dep, None)
