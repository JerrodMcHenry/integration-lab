"""Synchronization with PostgreSQL locks + history, across processes and through the API."""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_sync_history, get_sync_service
from app.main import app
from tests.support.mappings import MappingRegistry
from app.repositories.sync_history import PostgresSyncHistoryRepository
from tests.support.fakes import InMemorySyncHistoryRepository
from app.schemas.sync import SyncStage, SyncStatus
from app.services.account_sync import AccountSyncService, HistoryPersistenceError
from app.services.locks import PostgresAdvisorySyncLocks
from mock_crm import main as mock_crm
from mock_internal import main as mock_internal
from tests.support.data import CMS_KEY, MAPPING_DIR
from tests.integration import _workers
from tests.support.servers import LiveServer
from tests.support.concurrency import join_all, pool_results, start_threads, wait_event
from tests.support.fakes import StaticConnections

T = "meridian"


# --------------------------------------------------------------------------- #
# Live mock services on real sockets (shared by all worker processes)
# --------------------------------------------------------------------------- #


@pytest.fixture
def live_mocks():
    with LiveServer(mock_crm.create_app(app_env="test")) as crm, \
         LiveServer(mock_internal.create_app(app_env="test", api_key=CMS_KEY)) as cms:
        yield crm, cms


def pg_service(engine, crm_client, cms_client, history=None, lock_engine=None) -> AccountSyncService:
    return AccountSyncService(
        connections=StaticConnections(crm_client, cms_client), mappings=MappingRegistry(MAPPING_DIR),
        history=history or PostgresSyncHistoryRepository(engine),
        locks=PostgresAdvisorySyncLocks(lock_engine or engine, lock_timeout_seconds=5),
    )


# --------------------------------------------------------------------------- #
# Multi-process synchronization
# --------------------------------------------------------------------------- #
def test_concurrent_syncs_from_separate_processes(pg_url, pg_engine, live_mocks, tmp_path):
    crm, cms = live_mocks
    n = 6
    barrier = str(tmp_path / "go")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(n) as pool:
        pending = [pool.apply_async(_workers.run_sync,
                                    (pg_url, crm.url, cms.url, CMS_KEY, T, "SF-1001", barrier))
                   for _ in range(n)]
        time.sleep(2.0)
        open(barrier, "w").close()
        results = pool_results(pending, timeout=120, what=f"{n} sync processes")

    assert len({r["pid"] for r in results}) == n
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["created"] + ["unchanged"] * (n - 1), statuses
    assert len(cms.app.state.cms.customers) == 1
    assert cms.app.state.cms.write_count == 1  # only the creating process wrote

    history = PostgresSyncHistoryRepository(pg_engine).list_recent(T, crm_account_id="SF-1001")
    assert len(history) == n
    # History order (seq) matches lock order: the 'created' sync committed first.
    assert history[-1].status is SyncStatus.CREATED


def test_threads_in_one_process_also_serialised(pg_engine, crm_client, cms_client, cms_app):
    service = pg_service(pg_engine, crm_client, cms_client)
    results = []
    threads = start_threads([lambda: results.append(service.sync_account(T, "SF-1002"))] * 5)
    join_all(threads, timeout=60, what="5 concurrent syncs of SF-1002")
    assert sorted(r.status.value for r in results) == ["created"] + ["unchanged"] * 4
    assert len(cms_app.app.state.cms.customers) == 1


# --------------------------------------------------------------------------- #
# Semantics preserved with PostgreSQL backing
# --------------------------------------------------------------------------- #
def test_sync_semantics_unchanged_with_postgres(pg_engine, crm_client, cms_client, cms_app, crm_app):
    from tests.support.clients import cms_headers, crm_patch

    service = pg_service(pg_engine, crm_client, cms_client)
    created = service.sync_account(T, "SF-1002")
    cms_app.patch(f"/api/v1/customers/{created.destination_customer_id}",
                  json={"onboarding_status": "approved", "risk_classification": "high"},
                  headers=cms_headers())
    crm_patch(crm_app, "SF-1002", AnnualRevenue=9000000)
    cms_app.put("/_admin/faults", json={"mode": "error", "remaining": 100})
    failed = service.sync_account(T, "SF-1002")
    cms_app.put("/_admin/faults", json={"mode": "none"})
    updated = service.sync_account(T, "SF-1002")
    unchanged = service.sync_account(T, "SF-1002")

    assert [created.status, failed.status, updated.status, unchanged.status] == [
        SyncStatus.CREATED, SyncStatus.FAILED, SyncStatus.UPDATED, SyncStatus.UNCHANGED]
    assert updated.fields_changed == ["annual_revenue_usd", "crm_last_modified_at"]
    customer = cms_app.get("/api/v1/customers/by-external-id/SF-1002", headers=cms_headers()).json()
    assert (customer["onboarding_status"], customer["risk_classification"]) == ("approved", "high")

    stored = PostgresSyncHistoryRepository(pg_engine).list_recent(T)
    assert stored == [unchanged, updated, failed, created]
    assert stored[2].error.code == "destination_unavailable" and stored[2].error.retryable


# --------------------------------------------------------------------------- #
# Database unavailability
# --------------------------------------------------------------------------- #
def test_lock_database_down_fails_closed_before_any_upstream_call(
        dead_engine, crm_client, cms_client, crm_app, cms_app):
    history = InMemorySyncHistoryRepository()
    service = pg_service(dead_engine, crm_client, cms_client, history=history)
    record = service.sync_account(T, "SF-1001")
    assert record.status is SyncStatus.FAILED and record.stage is SyncStage.ACQUIRE_LOCK
    assert record.error.code == "database_unavailable" and record.error.retryable
    assert crm_app.app.state.crm.account_reads == 0
    assert cms_app.app.state.cms.write_count == 0
    assert history.list_recent(T) == [record]


def test_database_fully_down_raises_history_error_with_record(dead_engine, crm_client, cms_client, caplog):
    service = pg_service(dead_engine, crm_client, cms_client)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(HistoryPersistenceError) as exc_info:
            service.sync_account(T, "SF-1001")
    record = exc_info.value.record
    assert record.stage is SyncStage.ACQUIRE_LOCK
    assert "sync.history_write_failed" in caplog.text and str(record.id) in caplog.text


def test_history_write_fails_after_successful_sync(pg_engine, dead_engine, crm_client, cms_client,
                                                   cms_app, caplog):
    """Lock DB is fine but history storage is down: the outcome is surfaced, not swallowed."""
    service = pg_service(pg_engine, crm_client, cms_client,
                         history=PostgresSyncHistoryRepository(dead_engine))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(HistoryPersistenceError) as exc_info:
            service.sync_account(T, "SF-1001")
    assert exc_info.value.record.status is SyncStatus.CREATED
    assert len(cms_app.app.state.cms.customers) == 1  # the sync itself happened
    assert '"status":"created"' in caplog.text      # and is recoverable from logs
    # The lock was released despite the error.
    from tests.support.db import try_lock_elsewhere
    assert try_lock_elsewhere(str(pg_engine.url.render_as_string(hide_password=False)), T, "SF-1001")


# --------------------------------------------------------------------------- #
# API with PostgreSQL
# --------------------------------------------------------------------------- #
@pytest.fixture
def api_factory(auth):
    def make(service, history):
        app.dependency_overrides[get_sync_service] = lambda: service
        app.dependency_overrides[get_sync_history] = lambda: history
        return TestClient(app, headers=auth.headers("operator", "meridian"))

    yield make
    app.dependency_overrides.pop(get_sync_service, None)
    app.dependency_overrides.pop(get_sync_history, None)


def test_api_history_survives_restart(pg_url, pg_engine, crm_client, cms_client, api_factory):
    from app.db.engine import build_engine
    from tests.support.db import db_settings

    api = api_factory(pg_service(pg_engine, crm_client, cms_client), PostgresSyncHistoryRepository(pg_engine))
    r = api.post("/sync/accounts/SF-1001")
    assert r.status_code == 201 and r.headers["X-Sync-History-Persisted"] == "true"
    pg_engine.dispose()

    fresh = build_engine(db_settings(pg_url))  # "restarted" application
    try:
        api2 = api_factory(pg_service(fresh, crm_client, cms_client), PostgresSyncHistoryRepository(fresh))
        history = api2.get("/sync/history").json()
        assert [h["id"] for h in history] == [r.json()["id"]]
        assert api2.post("/sync/accounts/SF-1001").json()["status"] == "unchanged"
    finally:
        fresh.dispose()


def test_api_returns_503_when_database_down(dead_engine, crm_client, cms_client, api_factory):
    api = api_factory(pg_service(dead_engine, crm_client, cms_client),
                      PostgresSyncHistoryRepository(dead_engine))
    r = api.post("/sync/accounts/SF-1001")
    assert r.status_code == 503
    assert r.headers["X-Sync-History-Persisted"] == "false"
    assert r.json()["error"]["code"] == "database_unavailable"
    h = api.get("/sync/history")
    assert h.status_code == 503 and h.json() == {"detail": "database unavailable; retry later"}


def test_api_lock_timeout_returns_409(pg_engine, crm_client, cms_client, api_factory):
    service = AccountSyncService(
        connections=StaticConnections(crm_client, cms_client), mappings=MappingRegistry(MAPPING_DIR),
        history=PostgresSyncHistoryRepository(pg_engine),
        locks=PostgresAdvisorySyncLocks(pg_engine, lock_timeout_seconds=0.2))
    api = api_factory(service, PostgresSyncHistoryRepository(pg_engine))
    holder_in, release = threading.Event(), threading.Event()

    def holder():
        with PostgresAdvisorySyncLocks(pg_engine, lock_timeout_seconds=5).hold(T, "SF-1001"):
            holder_in.set()
            release.wait(10)

    threads = start_threads([holder], name="holder")
    try:
        wait_event(holder_in, timeout=5, what="holder to acquire the lock")
        r = api.post("/sync/accounts/SF-1001")
    finally:
        release.set()
        join_all(threads, timeout=15, what="lock holder to release")
    assert r.status_code == 409
    assert r.json()["error"] == {"code": "sync_lock_timeout",
                                 "message": "another sync of this account is still running",
                                 "retryable": True, "upstream_status": None, "issues": []}


def test_readiness_endpoint(monkeypatch, pg_engine, dead_engine):
    import app.main as main_module

    client = TestClient(app)
    monkeypatch.setattr(main_module, "get_engine", lambda: pg_engine)
    assert client.get("/health/ready").json() == {"status": "ready", "database": "ok"}
    monkeypatch.setattr(main_module, "get_engine", lambda: dead_engine)
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["database"] == "unavailable"
    assert client.get("/health").status_code == 200  # liveness unaffected
