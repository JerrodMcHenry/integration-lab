"""Retry classification, backoff bounds, deadline guard and write gate (no database)."""

from __future__ import annotations

import random
from contextlib import contextmanager

import pytest

from app.core.config import RetrySettings
from app.mapping.mapper import MappingRegistry
from app.repositories.audit import AuditEntry
from app.repositories.sync_history import InMemorySyncHistoryRepository
from app.schemas.sync import SyncStatus
from app.services.account_sync import AccountSyncService, OwnershipLost
from app.services.locks import LockLostError
from app.worker.retry_policy import RetryPolicy
from app.worker.runner import EventWorker, WorkerConfigurationError
from tests.conftest import MAPPING_DIR


def policy(base=5.0, cap=300.0, max_attempts=5, seed=1):
    return RetryPolicy(RetrySettings(base_delay_seconds=base, max_delay_seconds=cap), max_attempts, random.Random(seed))


@pytest.mark.parametrize("succeeded, retryable, attempt, expected", [
    (True, None, 1, "succeeded"), (True, None, 9, "succeeded"),
    (False, True, 1, "retry"), (False, True, 4, "retry"), (False, True, 5, "dead_letter"), (False, True, 7, "dead_letter"),
    (False, False, 1, "failed"), (False, False, 5, "failed"), (False, None, 1, "failed"),
])
def test_decision_table(succeeded, retryable, attempt, expected):
    assert policy().decide(succeeded=succeeded, retryable=retryable, attempt=attempt) == expected


def test_permanent_failures_are_never_retried():
    p = policy(max_attempts=20)
    assert all(p.decide(succeeded=False, retryable=False, attempt=a) == "failed" for a in range(1, 20))


def test_backoff_is_exponential_and_capped():
    p = policy(base=5, cap=60)
    assert [p.ceiling(a) for a in range(1, 7)] == [5, 10, 20, 40, 60, 60]
    assert p.ceiling(10_000) == 60  # no overflow, bounded


def test_jitter_stays_within_half_to_full_ceiling():
    p = policy(base=5, cap=300, seed=42)
    for attempt in range(1, 12):
        d = p.ceiling(attempt)
        samples = [p.delay(attempt) for _ in range(200)]
        assert all(d / 2 <= x <= d for x in samples)
        assert max(samples) - min(samples) > d * 0.2  # actually jittered, not constant


def test_jitter_decorrelates_concurrent_retries():
    delays = {round(policy(seed=s).delay(3), 3) for s in range(50)}
    assert len(delays) > 40


@pytest.mark.parametrize("kw", [{"base_delay_seconds": 0}, {"base_delay_seconds": 10, "max_delay_seconds": 5},
                                {"max_delay_seconds": 10 ** 6}])
def test_retry_settings_bounds(kw):
    with pytest.raises(ValueError):
        RetrySettings(**kw)


# --- processing deadline guard ------------------------------------------------ #
class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def worker(clock, lease=60.0, margin=10.0):
    return EventWorker(events=None, service=None, tenants=None, lease_seconds=lease, batch_size=1, max_attempts=5,
                       poll_interval=0.1, write_margin_seconds=margin, clock=clock)


def test_deadline_guard_allows_writes_well_inside_the_lease():
    clock = Clock()
    guard = worker(clock)._guard_for(deadline=clock.t + 60)
    clock.t += 49.9
    guard()  # 49.9 + 10 margin < 60


def test_deadline_guard_refuses_once_a_write_could_outlive_the_lease():
    clock = Clock()
    guard = worker(clock)._guard_for(deadline=clock.t + 60)
    clock.t += 50
    with pytest.raises(OwnershipLost, match="processing deadline"):
        guard()


def test_margin_must_be_shorter_than_lease():
    with pytest.raises(WorkerConfigurationError):
        worker(Clock(), lease=10, margin=10)


# --- write gate in the sync service ------------------------------------------- #
class LostLock:
    @contextmanager
    def hold(self, tenant_id, crm_account_id):
        class H:
            def verify(self):
                raise LockLostError("advisory lock no longer held by this session")
        yield H()


@pytest.mark.parametrize("make_svc, code", [
    (lambda crm, cms: AccountSyncService(crm=crm, destination=cms, mappings=MappingRegistry(MAPPING_DIR),
                                         history=InMemorySyncHistoryRepository(), locks=LostLock()), "sync_lock_lost"),
])
def test_lost_lock_blocks_destination_write(crm_client, cms_client, cms_app, make_svc, code):
    record = make_svc(crm_client, cms_client).sync_account("meridian", "SF-1001")
    assert record.status is SyncStatus.FAILED and record.error.code == code and record.error.retryable
    assert cms_app.app.state.cms.write_count == 0


def test_guard_blocks_destination_write(sync_service, cms_app):
    def expired():
        raise OwnershipLost("processing deadline reached")
    record = sync_service.sync_account("meridian", "SF-1001", guard=expired)
    assert record.error.code == "processing_deadline_exceeded" and record.error.retryable
    assert cms_app.app.state.cms.write_count == 0


def test_guard_checked_before_update_too(sync_service, cms_app, crm_app):
    from tests.conftest import crm_patch
    sync_service.sync_account("meridian", "SF-1001")
    crm_patch(crm_app, "SF-1001", Name="Changed")
    writes = cms_app.app.state.cms.write_count
    calls = []

    def guard():
        calls.append(1)
        raise OwnershipLost("processing deadline reached")
    record = sync_service.sync_account("meridian", "SF-1001", guard=guard)
    assert calls and record.error.code == "processing_deadline_exceeded"
    assert cms_app.app.state.cms.write_count == writes


def test_unchanged_sync_needs_no_write_so_guard_not_consulted(sync_service):
    sync_service.sync_account("meridian", "SF-1001")
    calls = []
    record = sync_service.sync_account("meridian", "SF-1001", guard=lambda: calls.append(1))
    assert record.status is SyncStatus.UNCHANGED and calls == []


# --- audit hygiene ---------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["client_secret", "access_token", "Authorization", "webhook_signature", "api_key"])
def test_audit_details_reject_sensitive_keys(key):
    with pytest.raises(ValueError):
        AuditEntry(actor="a", action="x.y", outcome="success", details={key: "v"})
