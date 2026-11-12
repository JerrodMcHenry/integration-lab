"""Background worker against real PostgreSQL: claiming, crashes, concurrency, failures, restarts."""

from __future__ import annotations

import multiprocessing
import os
import signal
import threading
import time

import pytest
from sqlalchemy import text

from app.db.engine import build_engine
from app.repositories.sync_history import PostgresSyncHistoryRepository, RepositoryUnavailableError
from app.repositories.webhook_events import PostgresWebhookEventRepository
from mock_crm import main as mock_crm
from mock_internal import main as mock_internal
from tests.integration import _workers
from tests.integration.conftest import db_settings
from tests.integration.test_pg_sync import LiveServer
from tests.integration.webhook_helpers import (
    ALL_SECRETS,
    build_service,
    build_worker,
    events,
    ingest_direct,
    seed_tenants,
)
from tests.test_tenant_isolation import World


@pytest.fixture
def world(pg_engine):
    seed_tenants(pg_engine)
    return World()  # in-process CRM/CMS per tenant, routed by base URL


@pytest.fixture
def service(pg_engine, world):
    return build_service(pg_engine, http_client_factory=world.route)


def by_id(engine):
    return {str(e["id"]): e for e in events(engine)}


# --------------------------------------------------------------------------- #
# Happy path and failure recording
# --------------------------------------------------------------------------- #
def test_worker_processes_event_through_existing_sync_service(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1, "SF-1002")
    worker = build_worker(pg_engine, service)
    assert worker.run_once() == 1
    ev = by_id(pg_engine)[eid]
    assert (ev["status"], ev["attempts"], ev["claim_token"], ev["lease_expires_at"]) == ("succeeded", 1, None, None)
    assert ev["processed_at"] is not None and ev["claimed_by"] == worker.worker_id
    record = PostgresSyncHistoryRepository(pg_engine).get("meridian", ev["sync_record_id"])
    assert record.status.value == "created" and record.crm_account_id == "SF-1002"
    assert [c["external_crm_id"] for c in world.cms_state("meridian").customers.values()] == ["SF-1002"]


def test_sync_failure_is_recorded_on_the_event(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1, "SF-404")
    build_worker(pg_engine, service).run_once()
    ev = by_id(pg_engine)[eid]
    assert ev["status"] == "failed" and ev["last_error_code"] == "crm_not_found"
    assert ev["last_error_retryable"] is False and ev["sync_record_id"] is not None


def test_retryable_upstream_failure_is_marked_retryable(pg_engine, world, service):
    world.crm["meridian"].state.crm.fault = mock_crm.FaultConfig(mode="error", remaining=100)
    [eid] = ingest_direct(pg_engine, 1)
    build_worker(pg_engine, service).run_once()
    ev = by_id(pg_engine)[eid]
    # M7: a retryable failure is rescheduled (pending, future available_at) instead of terminal 'failed'.
    assert (ev["status"], ev["last_error_code"], ev["last_error_retryable"]) == ("pending", "crm_unavailable", True)
    assert ev["available_at"] > ev["received_at"] and ev["processed_at"] is None


def test_tenant_isolation_in_worker(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1, "SF-1001", tenant="harbor", secret="whsec_harbor_test")
    build_worker(pg_engine, service).run_once()
    assert by_id(pg_engine)[eid]["last_error_code"] == "crm_not_found"  # Harbor's org has no SF-1001
    assert world.crm_state("meridian").account_reads == 0
    assert world.cms_state("meridian").customers == {}


def test_suspended_tenant_events_fail_without_syncing(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1)
    with pg_engine.begin() as c:
        c.execute(text("UPDATE tenants SET status='suspended' WHERE id='meridian'"))
    build_worker(pg_engine, service).run_once()
    assert by_id(pg_engine)[eid]["last_error_code"] == "tenant_inactive"
    assert world.crm_state("meridian").account_reads == 0


def test_ignored_events_are_never_claimed(pg_engine, world, service):
    from tests.integration.webhook_helpers import event_body, ingestor, signed
    body = event_body(event_type="contact.updated")
    ingestor(pg_engine).ingest("meridian", signed(body)["X-IntegrationLab-Signature"], body)
    assert build_worker(pg_engine, service).run_once() == 0


# --------------------------------------------------------------------------- #
# No transaction held during external HTTP calls
# --------------------------------------------------------------------------- #
def test_no_database_transaction_open_during_http_calls(pg_engine, pg_url, world):
    probe = build_engine(db_settings(pg_url), application_name="txn-probe")
    seen: list[list] = []

    def route(base_url):
        client = world.route(base_url)

        def check(_request):
            with probe.connect() as c:
                seen.append(c.execute(text(
                    "SELECT state FROM pg_stat_activity WHERE application_name = 'integrationlab-test' "
                    "AND state LIKE 'idle in transaction%'")).all())

        client.event_hooks = {"request": [check], "response": []}
        return client

    ingest_direct(pg_engine, 3, "SF-1001")
    build_worker(pg_engine, build_service(pg_engine, http_client_factory=route)).run(max_events=3, max_seconds=10)
    probe.dispose()
    assert len(seen) >= 6  # CRM token/account + CMS calls were observed
    assert all(rows == [] for rows in seen), f"transactions open during HTTP: {seen}"


# --------------------------------------------------------------------------- #
# Concurrent workers (separate OS processes)
# --------------------------------------------------------------------------- #
def test_concurrent_worker_processes_never_double_process(pg_engine, pg_url, tmp_path):
    with LiveServer(mock_crm.create_app(app_env="test", client_id="meridian-client",
                                        client_secret=ALL_SECRETS["IL_MERIDIAN__CRM_CLIENT_SECRET"])) as crm, \
         LiveServer(mock_internal.create_app(app_env="test",
                                             api_key=ALL_SECRETS["IL_MERIDIAN__CMS_API_KEY"])) as cms:
        seed_tenants(pg_engine, meridian_urls=(crm.url, cms.url))
        n = 30
        ids = ingest_direct(pg_engine, n // 2, "SF-1001") + ingest_direct(pg_engine, n // 2, "SF-1002")
        barrier = str(tmp_path / "go")
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(4) as pool:
            pending = [pool.apply_async(_workers.run_event_worker,
                                        (pg_url, ALL_SECRETS, f"w{i}", 6.0, barrier)) for i in range(4)]
            time.sleep(2.0)
            open(barrier, "w").close()
            results = [p.get(timeout=120) for p in pending]

        rows = by_id(pg_engine)
        assert sorted(rows) == sorted(ids)
        assert all(r["status"] == "succeeded" for r in rows.values()), {r["status"] for r in rows.values()}
        assert all(r["attempts"] == 1 for r in rows.values())             # never claimed twice
        assert sum(r["claimed"] for r in results) == n                     # each claim counted once
        assert len({r["claimed_by"] for r in rows.values()}) >= 2          # work really was shared
        assert len({r["pid"] for r in results}) == 4
        assert sum(r["lease_lost"] for r in results) == 0
        # Same-account events were serialised by the advisory lock: exactly one create per account.
        statuses = [PostgresSyncHistoryRepository(pg_engine).get("meridian", r["sync_record_id"]).status.value
                    for r in rows.values()]
        assert statuses.count("created") == 2 and statuses.count("unchanged") == n - 2
        assert len(cms.app.state.cms.customers) == 2


def test_skip_locked_claims_are_disjoint_under_contention(pg_engine, world):
    ingest_direct(pg_engine, 40)
    repo = PostgresWebhookEventRepository(pg_engine)
    claimed: list = []
    lock = threading.Lock()

    def claimer(i):
        while True:
            got = repo.claim(f"c{i}", batch=3, lease_seconds=60, max_attempts=5)
            if not got:
                return
            with lock:
                claimed.extend(e.id for e in got)

    threads = [threading.Thread(target=claimer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 40 and len(set(claimed)) == 40


# --------------------------------------------------------------------------- #
# Worker crashes and lease recovery
# --------------------------------------------------------------------------- #
def test_killed_worker_event_is_recovered_by_another_worker(pg_engine, pg_url, world, service, tmp_path):
    [eid] = ingest_direct(pg_engine, 1, "SF-1002")
    ready = tmp_path / "claimed"
    ctx = multiprocessing.get_context("spawn")
    doomed = ctx.Process(target=_workers.claim_and_hang, args=(pg_url, 2.0, str(ready)))
    doomed.start()
    deadline = time.time() + 30
    while not ready.exists():
        assert time.time() < deadline
        time.sleep(0.05)
    assert by_id(pg_engine)[eid]["status"] == "processing"
    os.kill(doomed.pid, signal.SIGKILL)  # crash mid-processing: no completion, no cleanup
    doomed.join(10)

    survivor = build_worker(pg_engine, service)
    assert survivor.run_once() == 0  # lease still valid: not stolen early
    time.sleep(2.2)
    assert survivor.run_once() == 1  # lease expired: reclaimed
    ev = by_id(pg_engine)[eid]
    assert (ev["status"], ev["attempts"], ev["claimed_by"]) == ("succeeded", 2, survivor.worker_id)


def test_crash_loop_is_bounded_by_max_attempts(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1)
    repo = PostgresWebhookEventRepository(pg_engine)
    for _ in range(2):  # two claims that never complete (crashes)
        assert len(repo.claim("crashy", batch=1, lease_seconds=1, max_attempts=2)) == 1
        time.sleep(1.1)
    assert build_worker(pg_engine, service, max_attempts=2).run_once() == 0
    ev = by_id(pg_engine)[eid]
    # M7: exhausted claims go to the dead-letter state (M6: terminal 'failed').
    assert (ev["status"], ev["last_error_code"], ev["attempts"]) == ("dead_letter", "worker_attempts_exhausted", 2)
    assert ev["dead_lettered_at"] is not None
    assert ev["last_error_retryable"] is False


def test_stale_worker_cannot_overwrite_after_lease_lost(pg_engine, world, service):
    from app.repositories.webhook_events import Outcome
    [eid] = ingest_direct(pg_engine, 1)
    repo = PostgresWebhookEventRepository(pg_engine)
    late = Outcome("failed", error_code="late", error_message="x", error_retryable=False)
    [stale] = repo.claim("slow-worker", batch=1, lease_seconds=1, max_attempts=5)
    time.sleep(1.1)
    [owner] = repo.claim("new-owner", batch=1, lease_seconds=60, max_attempts=5)
    # The stale worker finishes while the new owner is still processing: must be fenced off.
    assert repo.complete(stale, late) is False
    ev = by_id(pg_engine)[eid]
    assert (ev["status"], ev["claimed_by"], ev["last_error_code"]) == ("processing", "new-owner", None)
    assert repo.complete(owner, Outcome("succeeded")) is True
    # And after the owner finished, a late completion is still rejected.
    assert repo.complete(stale, late) is False
    assert by_id(pg_engine)[eid]["status"] == "succeeded"


def test_graceful_stop_leaves_unprocessed_claims_recoverable(pg_engine, world, service):
    ingest_direct(pg_engine, 3)
    worker = build_worker(pg_engine, service, batch=3, lease=1)
    original = worker._process

    def process_then_stop(event):
        original(event)
        worker.stop()  # SIGTERM arrives after the first event

    worker._process = process_then_stop
    worker.run_once()
    statuses = sorted(e["status"] for e in events(pg_engine))
    assert statuses == ["processing", "processing", "succeeded"]
    time.sleep(1.1)
    build_worker(pg_engine, service).run(max_events=2, max_seconds=10)
    assert sorted(e["status"] for e in events(pg_engine)) == ["succeeded"] * 3


# --------------------------------------------------------------------------- #
# Database failures
# --------------------------------------------------------------------------- #
def test_claim_failure_backs_off_and_worker_survives(dead_engine, pg_engine, service):
    worker = build_worker(dead_engine, service)
    with pytest.raises(RepositoryUnavailableError):
        worker.run_once()
    stats = worker.run(max_seconds=0.5)  # loop must not crash
    assert stats.claim_errors >= 2 and stats.claimed == 0


def test_completion_failure_keeps_event_for_recovery(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1, "SF-1002")
    worker = build_worker(pg_engine, service, lease=1)

    class BrokenComplete(PostgresWebhookEventRepository):
        def complete(self, event, outcome):
            raise RepositoryUnavailableError("database unavailable: OperationalError")

    worker._events = BrokenComplete(pg_engine)
    worker.run_once()
    assert worker.stats.completion_errors == 1
    assert by_id(pg_engine)[eid]["status"] == "processing"  # not lost, not falsely completed
    time.sleep(1.1)
    build_worker(pg_engine, service).run_once()
    ev = by_id(pg_engine)[eid]
    assert (ev["status"], ev["attempts"]) == ("succeeded", 2)
    # Re-running the sync was idempotent: one customer, second sync 'unchanged'.
    assert len(world.cms_state("meridian").customers) == 1
    assert PostgresSyncHistoryRepository(pg_engine).get("meridian", ev["sync_record_id"]).status.value == "unchanged"


def test_history_write_failure_is_recorded_as_retryable(pg_engine, world, dead_engine):
    svc = build_service(pg_engine, http_client_factory=world.route)
    svc._history = PostgresSyncHistoryRepository(dead_engine)
    [eid] = ingest_direct(pg_engine, 1)
    build_worker(pg_engine, svc).run_once()
    ev = by_id(pg_engine)[eid]
    # M7: retryable -> rescheduled (M6: terminal 'failed').
    assert (ev["status"], ev["last_error_code"], ev["last_error_retryable"]) == (
        "pending", "history_not_persisted", True)
    assert ev["available_at"] > ev["received_at"]


# --------------------------------------------------------------------------- #
# Survives application and worker restarts
# --------------------------------------------------------------------------- #
def test_events_survive_restart_and_are_processed_by_a_new_worker_process(pg_engine, pg_url, tmp_path):
    with LiveServer(mock_crm.create_app(app_env="test", client_id="meridian-client",
                                        client_secret=ALL_SECRETS["IL_MERIDIAN__CRM_CLIENT_SECRET"])) as crm, \
         LiveServer(mock_internal.create_app(app_env="test",
                                             api_key=ALL_SECRETS["IL_MERIDIAN__CMS_API_KEY"])) as cms:
        seed_tenants(pg_engine, meridian_urls=(crm.url, cms.url))
        ids = ingest_direct(pg_engine, 5, "SF-1001")
        pg_engine.dispose()  # "API" shuts down; nothing in memory survives
        barrier = tmp_path / "go"
        barrier.touch()
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(1) as pool:  # brand-new process
            result = pool.apply(_workers.run_event_worker, (pg_url, ALL_SECRETS, "fresh", 4.0, str(barrier)))
        assert result["succeeded"] == 5
        assert all(by_id(pg_engine)[i]["status"] == "succeeded" for i in ids)
