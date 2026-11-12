"""PostgresSyncHistoryRepository: persistence, transactions, availability."""

from __future__ import annotations

import multiprocessing
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db.engine import build_engine
from app.repositories.sync_history import (
    InMemorySyncHistoryRepository,
    PostgresSyncHistoryRepository,
    RepositoryError,
    RepositoryUnavailableError,
)
from app.schemas.sync import SyncErrorDetail, SyncRecord, SyncStage, SyncStatus
from tests.integration import _workers
from tests.integration.conftest import db_settings

T = "meridian"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def rec(account="SF-1", status=SyncStatus.UPDATED, tenant=T, **kw) -> SyncRecord:
    values = dict(tenant_id=tenant, crm_account_id=account, status=status, started_at=NOW,
                  finished_at=NOW + timedelta(milliseconds=5), duration_ms=5, mapping_version=1)
    values.update(kw)
    return SyncRecord(**values)


def failed(account="SF-1", **kw) -> SyncRecord:
    values = dict(stage=SyncStage.TRANSFORM, error=SyncErrorDetail(
        code="mapping_failed", message="Source record failed mapping validation", retryable=False,
        issues=[{"field": "legal_name", "code": "missing_required_field", "message": "missing"}]))
    values.update(kw)
    return rec(account, SyncStatus.FAILED, **values)


def count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM sync_records")).scalar()


def test_roundtrip_preserves_every_field(pg_engine):
    repo = PostgresSyncHistoryRepository(pg_engine)
    original = rec(fields_changed=["annual_revenue_usd", "legal_name"],
                   destination_customer_id="CUS-123",
                   source_last_modified=datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    bad = failed("SF-2", error=SyncErrorDetail(code="destination_unavailable", message="down",
                                              retryable=True, upstream_status=503),
                 stage=SyncStage.LOAD_DESTINATION)
    repo.add(original)
    repo.add(bad)
    assert repo.get(T, original.id) == original
    assert repo.get(T, bad.id) == bad


def test_newest_first_with_filters_and_limit(pg_engine):
    repo = PostgresSyncHistoryRepository(pg_engine)
    records = [rec("A"), failed("B"), rec("A", SyncStatus.UNCHANGED), failed("A")]
    for r in records:
        repo.add(r)
    assert [r.id for r in repo.list_recent(T)] == [r.id for r in reversed(records)]
    assert [r.id for r in repo.list_recent(T, crm_account_id="A")] == [
        records[3].id, records[2].id, records[0].id]
    assert [r.id for r in repo.list_recent(T, status=SyncStatus.FAILED)] == [records[3].id, records[1].id]
    assert len(repo.list_recent(T, limit=2)) == 2
    assert len(repo.list_recent(T, limit=10_000)) == 4  # clamped, not an error


def test_same_semantics_as_in_memory_repository(pg_engine):
    pg, mem = PostgresSyncHistoryRepository(pg_engine), InMemorySyncHistoryRepository()
    records = [rec("A"), failed("B"), rec("C", tenant="other"), rec("A", SyncStatus.UNCHANGED)]
    for r in records:
        pg.add(r)
        mem.add(r)
    for kwargs in ({}, {"crm_account_id": "A"}, {"status": SyncStatus.FAILED}, {"limit": 1}):
        assert pg.list_recent(T, **kwargs) == mem.list_recent(T, **kwargs)
    assert pg.get("other", records[0].id) is None and mem.get("other", records[0].id) is None


def test_tenant_isolation(pg_engine):
    repo = PostgresSyncHistoryRepository(pg_engine)
    mine, theirs = rec(), rec(tenant="other-tenant")
    repo.add(mine)
    repo.add(theirs)
    assert [r.id for r in repo.list_recent(T)] == [mine.id]
    assert repo.get(T, theirs.id) is None


def test_persists_across_engine_restart(pg_url, pg_engine):
    record = rec()
    PostgresSyncHistoryRepository(pg_engine).add(record)
    pg_engine.dispose()  # all connections closed, as on shutdown

    restarted = build_engine(db_settings(pg_url))
    try:
        assert PostgresSyncHistoryRepository(restarted).get(T, record.id) == record
    finally:
        restarted.dispose()


def test_persists_across_process_restart(pg_url, pg_engine):
    """A separate OS process writes and exits; this process reads it back."""
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(1) as pool:
        record_id = pool.apply(_workers.write_then_exit, (pg_url, T, "SF-PROC"))
    found = PostgresSyncHistoryRepository(pg_engine).list_recent(T, crm_account_id="SF-PROC")
    assert [str(r.id) for r in found] == [record_id]


def test_failed_insert_rolls_back_and_pool_stays_healthy(pg_engine):
    repo = PostgresSyncHistoryRepository(pg_engine)
    ok = rec()
    repo.add(ok)
    with pytest.raises(RepositoryError) as exc_info:
        repo.add(ok)  # duplicate primary key
    assert not isinstance(exc_info.value, RepositoryUnavailableError)
    # Constraint violation from a record that bypassed Pydantic validation.
    invalid = SyncRecord.model_construct(**{**rec().model_dump(), "status": SyncStatus.FAILED,
                                            "stage": None, "error": None})
    with pytest.raises(RepositoryError):
        repo.add(invalid)
    assert count(pg_engine) == 1
    # Connections went back to the pool cleanly (no 'current transaction is aborted').
    for _ in range(pg_engine.pool.size() + 2):
        repo.add(rec())
    assert count(pg_engine) == 1 + pg_engine.pool.size() + 2


def test_long_error_message_truncated_not_rejected(pg_engine):
    repo = PostgresSyncHistoryRepository(pg_engine)
    r = rec(status=SyncStatus.FAILED, stage=SyncStage.INTERNAL,
            error=SyncErrorDetail(code="x", message="m" * 5000, retryable=False))
    repo.add(r)
    assert len(repo.get(T, r.id).error.message) == 1000


def test_database_unavailable_raises_typed_error(dead_engine):
    repo = PostgresSyncHistoryRepository(dead_engine)
    with pytest.raises(RepositoryUnavailableError):
        repo.add(rec())
    with pytest.raises(RepositoryUnavailableError):
        repo.list_recent(T)
    with pytest.raises(RepositoryUnavailableError):
        repo.get(T, rec().id)


def test_recovers_after_server_kills_connections(pg_engine, pg_url):
    """Simulates a DB restart/failover: pooled connections are terminated server-side."""
    repo = PostgresSyncHistoryRepository(pg_engine)
    repo.add(rec())
    admin = build_engine(db_settings(pg_url), application_name="integrationlab-admin")
    try:
        with admin.begin() as conn:
            killed = conn.execute(text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                "WHERE application_name = 'integrationlab-test' AND pid <> pg_backend_pid()")).scalar()
    finally:
        admin.dispose()
    assert killed >= 1
    repo.add(rec())  # pool_pre_ping discards the dead connection and reconnects
    assert count(pg_engine) == 2
