"""Lock/history failure handling and wiring, without a database."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api.deps import build_lock_provider, get_sync_history, get_sync_service, worst_case_sync_seconds
from app.core.config import ConfigurationError, DatabaseSettings, load_settings
from app.main import app
from app.mapping.mapper import MappingRegistry
from app.repositories.sync_history import InMemorySyncHistoryRepository
from app.schemas.sync import SyncStage, SyncStatus
from app.services.account_sync import AccountSyncService, HistoryPersistenceError
from app.services.locks import (
    InProcessSyncLocks,
    LockTimeoutError,
    LockUnavailableError,
    PostgresAdvisorySyncLocks,
    advisory_lock_key,
)
from tests.conftest import MAPPING_DIR, cms_settings, crm_settings

T = "meridian"


class RaisingLocks:
    def __init__(self, exc):
        self.exc = exc

    @contextmanager
    def hold(self, tenant_id, crm_account_id):
        raise self.exc
        yield  # pragma: no cover


class BrokenHistory(InMemorySyncHistoryRepository):
    def add(self, record):
        raise RuntimeError("disk on fire")


def service(crm_client, cms_client, *, locks=None, history=None):
    return AccountSyncService(crm=crm_client, destination=cms_client,
                              mappings=MappingRegistry(MAPPING_DIR),
                              history=history or InMemorySyncHistoryRepository(), locks=locks)


@pytest.mark.parametrize("exc, code", [(LockTimeoutError("busy"), "sync_lock_timeout"),
                                       (LockUnavailableError("down"), "database_unavailable")])
def test_lock_failure_recorded_without_touching_upstreams(crm_client, cms_client, crm_app, exc, code):
    history = InMemorySyncHistoryRepository()
    record = service(crm_client, cms_client, locks=RaisingLocks(exc), history=history).sync_account(T, "SF-1001")
    assert record.status is SyncStatus.FAILED and record.stage is SyncStage.ACQUIRE_LOCK
    assert record.error.code == code and record.error.retryable is True
    assert crm_app.app.state.crm.account_reads == 0
    assert history.list_recent(T) == [record]


def test_history_failure_raises_with_record_and_logs_it(crm_client, cms_client, cms_app, caplog):
    svc = service(crm_client, cms_client, history=BrokenHistory())
    with pytest.raises(HistoryPersistenceError) as exc_info:
        svc.sync_account(T, "SF-1001")
    assert exc_info.value.record.status is SyncStatus.CREATED
    assert len(cms_app.app.state.cms.customers) == 1
    assert str(exc_info.value.record.id) in caplog.text
    assert "Acme Capital" not in caplog.text  # still no account data


@pytest.fixture
def api_with(auth):
    def make(svc):
        app.dependency_overrides[get_sync_service] = lambda: svc
        app.dependency_overrides[get_sync_history] = lambda: InMemorySyncHistoryRepository()
        return TestClient(app, headers=auth.headers("operator"))
    yield make
    app.dependency_overrides.pop(get_sync_service, None)
    app.dependency_overrides.pop(get_sync_history, None)


@pytest.mark.parametrize("exc, status", [(LockTimeoutError("busy"), 409), (LockUnavailableError("down"), 503)])
def test_api_maps_lock_failures(api_with, crm_client, cms_client, exc, status):
    r = api_with(service(crm_client, cms_client, locks=RaisingLocks(exc))).post("/sync/accounts/SF-1001")
    assert r.status_code == status
    assert r.json()["stage"] == "acquire_lock"
    assert r.headers["X-Sync-History-Persisted"] == "true"


def test_api_history_failure_is_503_with_header(api_with, crm_client, cms_client):
    r = api_with(service(crm_client, cms_client, history=BrokenHistory())).post("/sync/accounts/SF-1001")
    assert r.status_code == 503
    assert r.headers["X-Sync-History-Persisted"] == "false"
    assert r.json()["status"] == "created"  # caller learns the destination WAS updated


def test_lock_key_is_stable_signed_64bit_and_scoped():
    k = advisory_lock_key("meridian", "SF-1")
    assert k == advisory_lock_key("meridian", "SF-1")
    assert -(2**63) <= k < 2**63
    assert k != advisory_lock_key("meridian", "SF-2")
    assert k != advisory_lock_key("other", "SF-1")
    # NUL separator: ("a", "bc") and ("ab", "c") must not collide.
    assert advisory_lock_key("a", "bc") != advisory_lock_key("ab", "c")


def test_in_process_locks_used_without_database():
    assert isinstance(build_lock_provider(load_settings(), None), InProcessSyncLocks)


def test_lock_backstop_exceeds_worst_case_sync_duration():
    crm, cms = crm_settings(timeout_seconds=30, max_retries=5, backoff_max_seconds=10), \
        cms_settings(timeout_seconds=30, max_retries=5, backoff_max_seconds=10)
    worst = worst_case_sync_seconds(crm, cms)
    settings = load_settings()
    settings = type(settings)(**{**settings.__dict__, "crm": crm, "internal": cms})
    provider = build_lock_provider(settings, engine=object())  # engine unused until hold()
    assert isinstance(provider, PostgresAdvisorySyncLocks)
    assert provider._idle_timeout_ms / 1000 >= 2 * worst


@pytest.mark.parametrize("kwargs", [{"url": "mysql://x"}, {"url": None, "pool_size": 0},
                                    {"url": None, "lock_timeout_seconds": 0},
                                    {"url": None, "lock_timeout_seconds": 301}])
def test_database_settings_validation(kwargs):
    with pytest.raises(ValueError):
        DatabaseSettings(**kwargs)


def test_production_requires_database_url(monkeypatch):
    for k, v in {"APP_ENV": "production", "CRM_BASE_URL": "https://crm.example.com",
                 "CRM_CLIENT_SECRET": "real", "INTERNAL_API_BASE_URL": "https://cms.example.com",
                 "INTERNAL_API_KEY": "real", "DATABASE_URL": ""}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CRM_TOKEN_URL", raising=False)
    with pytest.raises(ConfigurationError, match="DATABASE_URL is required"):
        load_settings()
