"""Retries, backoff, dead-lettering, lease expiry, deadline guard and lock checks (real PostgreSQL)."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.config import RetrySettings
from app.repositories.operations import PostgresOperationsRepository
from app.repositories.webhook_events import Outcome, PostgresWebhookEventRepository
from app.worker.retry_policy import RetryPolicy
from tests.support.webhooks import attempts, by_id, crm_fault, drain, fast_policy
from tests.support.webhooks import build_service, build_worker, ingest_direct
from tests.support.concurrency import join_all, start_threads


# --------------------------------------------------------------------------- #
# Scheduling and backoff
# --------------------------------------------------------------------------- #
def test_transient_failure_schedules_a_jittered_retry(pg_engine, world, service):
    crm_fault(world, 100)
    [eid] = ingest_direct(pg_engine, 1)
    p = RetryPolicy(RetrySettings(base_delay_seconds=30, max_delay_seconds=300), 5)
    w = build_worker(pg_engine, service, retry_policy=p)
    w.run_once()
    ev = by_id(pg_engine)[eid]
    with pg_engine.connect() as c:
        db_now = c.execute(text("SELECT now()")).scalar()
    delay = (ev["available_at"] - db_now).total_seconds()
    assert ev["status"] == "pending" and ev["attempts"] == 1 and ev["claim_token"] is None
    assert 13 <= delay <= 30.5  # [d/2, d] with d=30s for attempt 1 (minus elapsed)
    [a] = attempts(pg_engine, eid)
    assert (a["attempt"], a["outcome"], a["error_code"], a["retryable"]) == (1, "retry_scheduled", "crm_unavailable", True)
    assert a["next_attempt_at"] == ev["available_at"]
    assert w.run_once() == 0  # backoff respected: not claimable yet


def test_exhausted_retries_go_to_dead_letter_with_full_history(pg_engine, world, service):
    crm_fault(world, 100)
    [eid] = ingest_direct(pg_engine, 1)
    drain(pg_engine, service, policy=fast_policy(max_attempts=4),
          until=lambda: by_id(pg_engine)[eid]["status"] == "dead_letter")
    ev = by_id(pg_engine)[eid]
    assert ev["attempts"] == 4 and ev["dead_lettered_at"] is not None and ev["last_error_retryable"] is True
    hist = attempts(pg_engine, eid)
    assert [h["outcome"] for h in hist] == ["retry_scheduled"] * 3 + ["dead_letter"]
    assert [h["attempt"] for h in hist] == [1, 2, 3, 4]
    # Each retry waited at least half of its exponential ceiling.
    p = fast_policy()
    for prev, nxt, n in zip(hist, hist[1:], range(1, 4)):
        assert nxt["finished_at"] - prev["finished_at"] >= timedelta(seconds=p.ceiling(n) / 2)


def test_recovery_after_transient_outage(pg_engine, world, service):
    crm_fault(world, 6)  # 2 attempts x (1 + 2 in-client retries) fail, then the CRM recovers
    [eid] = ingest_direct(pg_engine, 1, "SF-1002")
    drain(pg_engine, service, policy=fast_policy(max_attempts=5),
          until=lambda: by_id(pg_engine)[eid]["status"] == "succeeded")
    assert [h["outcome"] for h in attempts(pg_engine, eid)] == ["retry_scheduled", "retry_scheduled", "succeeded"]
    assert len(world.cms_state("meridian").customers) == 1


@pytest.mark.parametrize("account, code", [("SF-404", "crm_not_found")])
def test_permanent_failure_is_not_retried(pg_engine, world, service, account, code):
    [eid] = ingest_direct(pg_engine, 1, account)
    w = build_worker(pg_engine, service, retry_policy=fast_policy())
    w.run_once()
    time.sleep(0.8)
    assert w.run_once() == 0
    ev = by_id(pg_engine)[eid]
    assert (ev["status"], ev["last_error_code"], ev["last_error_retryable"], ev["attempts"]) == ("failed", code, False, 1)
    assert [h["outcome"] for h in attempts(pg_engine, eid)] == ["failed"]


def test_mapping_failure_is_permanent(pg_engine, world, service):
    world.crm["meridian"].state.crm.accounts["SF-BAD"] = {"Id": "SF-BAD", "Name": ""}
    [eid] = ingest_direct(pg_engine, 1, "SF-BAD")
    build_worker(pg_engine, service, retry_policy=fast_policy()).run_once()
    assert (by_id(pg_engine)[eid]["status"], by_id(pg_engine)[eid]["last_error_code"]) == ("failed", "mapping_failed")


# --------------------------------------------------------------------------- #
# Lease expiry, crashes, stale workers
# --------------------------------------------------------------------------- #
def test_lease_expiry_is_recorded_in_history(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1)
    repo = PostgresWebhookEventRepository(pg_engine)
    repo.claim("crashed-worker", batch=1, lease_seconds=1, max_attempts=5)
    time.sleep(1.1)
    build_worker(pg_engine, service, worker_id="survivor").run_once()
    hist = attempts(pg_engine, eid)
    assert [(h["attempt"], h["outcome"], h["worker_id"]) for h in hist] == [
        (1, "lease_expired", "crashed-worker"), (2, "succeeded", "survivor")]


def test_crash_loop_dead_letters_with_lease_history(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1)
    repo = PostgresWebhookEventRepository(pg_engine)
    for _ in range(2):
        repo.claim("crashy", batch=1, lease_seconds=1, max_attempts=2)
        time.sleep(1.1)
    build_worker(pg_engine, service, max_attempts=2).run_once()
    assert by_id(pg_engine)[eid]["status"] == "dead_letter"
    assert [h["outcome"] for h in attempts(pg_engine, eid)] == ["lease_expired", "lease_expired"]


def test_stale_worker_cannot_reschedule_or_write_history(pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1)
    repo = PostgresWebhookEventRepository(pg_engine)
    [stale] = repo.claim("stale", batch=1, lease_seconds=1, max_attempts=5)
    time.sleep(1.1)
    build_worker(pg_engine, service, worker_id="owner").run_once()
    late = Outcome("pending", error_code="crm_unavailable", error_message="x", error_retryable=True,
                   retry_delay_seconds=5)
    assert repo.complete(stale, late) is False
    ev = by_id(pg_engine)[eid]
    assert ev["status"] == "succeeded"
    assert [h["worker_id"] for h in attempts(pg_engine, eid)] == ["stale", "owner"]  # lease_expired, succeeded only


def test_processing_deadline_blocks_destination_write(pg_engine, world, service):
    """A worker that stalls past its safe deadline must not write, even though the DB lease is still valid."""
    [eid] = ingest_direct(pg_engine, 1, "SF-1002")
    ticks = iter([0.0] + [100.0] * 1000)  # claim at t=0; every later reading is past lease (30) - margin

    w = build_worker(pg_engine, service, lease=30, write_margin_seconds=5, clock=lambda: next(ticks),
                     retry_policy=fast_policy())
    w.run_once()
    ev = by_id(pg_engine)[eid]
    assert (ev["status"], ev["last_error_code"], ev["last_error_retryable"]) == (
        "pending", "processing_deadline_exceeded", True)
    assert world.cms_state("meridian").customers == {}  # no destination write happened


def test_lost_advisory_lock_detected_before_destination_write(pg_engine, pg_url, world):
    """Kill the lock session after the CRM read but before the CMS write: the write is refused."""
    from app.db.engine import build_engine
    from tests.support.db import db_settings
    admin = build_engine(db_settings(pg_url), application_name="killer")
    killed = []

    def route(base_url):
        client = world.route(base_url)
        if "crm-" in base_url:
            def kill_lock_after_account_read(response):
                if "/crm/accounts/" in str(response.request.url) and not killed:
                    with admin.begin() as c:
                        c.execute(text("SELECT pg_terminate_backend(l.pid) FROM pg_locks l "
                                       "JOIN pg_stat_activity a USING (pid) WHERE l.locktype = 'advisory' "
                                       "AND a.application_name = 'integrationlab-test'"))
                    killed.append(True)
            client.event_hooks = {"request": [], "response": [kill_lock_after_account_read]}
        return client

    [eid] = ingest_direct(pg_engine, 1, "SF-1002")
    build_worker(pg_engine, build_service(pg_engine, http_client_factory=route), retry_policy=fast_policy()).run_once()
    admin.dispose()
    ev = by_id(pg_engine)[eid]
    assert killed and (ev["status"], ev["last_error_code"]) == ("pending", "sync_lock_lost")
    assert world.cms_state("meridian").customers == {}


# --------------------------------------------------------------------------- #
# Concurrent workers with transient failures
# --------------------------------------------------------------------------- #
def test_concurrent_workers_with_retries_process_each_event_exactly_to_completion(pg_engine, world, service):
    ids = ingest_direct(pg_engine, 12, "SF-1001") + ingest_direct(pg_engine, 12, "SF-1002")
    crm_fault(world, 20)  # a burst of transient CRM errors shared by everyone
    p = fast_policy(max_attempts=8)
    workers = [build_worker(pg_engine, service, retry_policy=p, worker_id=f"t{i}", batch=2) for i in range(4)]
    done = lambda: all(r["status"] == "succeeded" for r in by_id(pg_engine).values())  # noqa: E731
    stop = time.time() + 30

    def loop(w):
        while time.time() < stop and not done():
            w.run_once()
            time.sleep(0.02)

    threads = start_threads([lambda w=w: loop(w) for w in workers], name="worker")
    join_all(threads, timeout=60, what="4 worker threads draining 24 events (loop deadline 30s)")
    rows = by_id(pg_engine)
    assert done(), {r["status"] for r in rows.values()}
    assert sum(w.stats.retried for w in workers) >= 1
    for eid in ids:
        hist = attempts(pg_engine, eid)
        assert [h["attempt"] for h in hist] == list(range(1, len(hist) + 1))  # no duplicate attempt numbers
        assert hist[-1]["outcome"] == "succeeded" and all(h["outcome"] == "retry_scheduled" for h in hist[:-1])
    assert len(world.cms_state("meridian").customers) == 2


# --------------------------------------------------------------------------- #
# Database outages
# --------------------------------------------------------------------------- #
def test_heartbeat_failure_does_not_stop_processing(pg_engine, world, service):
    from app.db.errors import RepositoryUnavailableError
    [eid] = ingest_direct(pg_engine, 1)
    w = build_worker(pg_engine, service)

    class NoHeartbeat(PostgresOperationsRepository):
        def heartbeat(self, **kw):
            raise RepositoryUnavailableError("down")

    w._operations = NoHeartbeat(pg_engine)
    w.run_once()
    assert by_id(pg_engine)[eid]["status"] == "succeeded"


def test_worker_heartbeats_recorded(pg_engine, world, service):
    ingest_direct(pg_engine, 2)
    w = build_worker(pg_engine, service, worker_id="hb-worker")
    w.run(max_events=2, max_seconds=5)
    with pg_engine.connect() as c:
        row = c.execute(text("SELECT state, events_succeeded FROM worker_heartbeats WHERE worker_id='hb-worker'")).one()
    assert (row.state, row.events_succeeded) == ("stopped", 2)
