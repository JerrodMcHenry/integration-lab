"""PostgreSQL advisory locks: lifecycle, timeouts, crash release, multi-process."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time

import pytest
from sqlalchemy import text

from app.db.engine import build_engine
from app.services.locks import (
    LockTimeoutError,
    LockUnavailableError,
    PostgresAdvisorySyncLocks,
    advisory_lock_key,
)
from tests.integration import _workers
from tests.support.db import db_settings, try_lock_elsewhere
from tests.support.concurrency import join_all, pool_results, start_threads, wait_event, wait_until

T = "meridian"


def locks(engine, timeout=5.0):
    return PostgresAdvisorySyncLocks(engine, lock_timeout_seconds=timeout)


def advisory_locks_held(engine, key: int) -> int:
    hi, lo = (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid::bigint = :hi AND objid::bigint = :lo"), {"hi": hi, "lo": lo}).scalar()


def test_lock_held_during_body_and_released_after(pg_engine, pg_url):
    key = advisory_lock_key(T, "SF-1")
    with locks(pg_engine).hold(T, "SF-1"):
        assert advisory_locks_held(pg_engine, key) == 1
        assert try_lock_elsewhere(pg_url, T, "SF-1") is False
    assert advisory_locks_held(pg_engine, key) == 0
    assert try_lock_elsewhere(pg_url, T, "SF-1") is True


def test_released_when_body_raises(pg_engine, pg_url):
    with pytest.raises(ValueError):
        with locks(pg_engine).hold(T, "SF-1"):
            raise ValueError("sync blew up")
    assert try_lock_elsewhere(pg_url, T, "SF-1") is True


def test_body_exceptions_propagate_unchanged(pg_engine):
    from sqlalchemy.exc import OperationalError

    with pytest.raises(OperationalError):
        with locks(pg_engine).hold(T, "SF-1"):
            raise OperationalError("stmt", {}, Exception("from body"))


def test_scope_is_tenant_and_account(pg_engine, pg_url):
    with locks(pg_engine).hold(T, "SF-1"):
        assert try_lock_elsewhere(pg_url, T, "SF-2") is True          # other account
        assert try_lock_elsewhere(pg_url, "other", "SF-1") is True    # other tenant
        assert try_lock_elsewhere(pg_url, T, "SF-1") is False


def test_second_holder_waits_then_proceeds(pg_engine):
    provider = locks(pg_engine)
    events: list[str] = []
    inside = threading.Event()

    def first():
        with provider.hold(T, "SF-1"):
            events.append("first-in")
            inside.set()
            time.sleep(0.4)
            events.append("first-out")

    threads = start_threads([first], name="first-holder")
    try:
        wait_event(inside, timeout=5, what="first holder to acquire the lock")
        started = time.monotonic()
        with provider.hold(T, "SF-1"):
            events.append("second-in")
            waited = time.monotonic() - started
    finally:
        join_all(threads, timeout=15, what="first lock holder to finish")
    assert events == ["first-in", "first-out", "second-in"]
    assert waited >= 0.25


def test_lock_timeout(pg_engine):
    holder_in, release = threading.Event(), threading.Event()

    def holder():
        with locks(pg_engine).hold(T, "SF-1"):
            holder_in.set()
            release.wait(10)

    threads = start_threads([holder], name="holder")
    try:
        wait_event(holder_in, timeout=5, what="holder to acquire the lock")
        started = time.monotonic()
        with pytest.raises(LockTimeoutError) as exc_info:
            with locks(pg_engine, timeout=0.3).hold(T, "SF-1"):
                pass
        elapsed = time.monotonic() - started
    finally:
        release.set()
        join_all(threads, timeout=15, what="lock holder to release")
    assert exc_info.value.retryable and exc_info.value.code == "sync_lock_timeout"
    assert 0.25 <= elapsed < 2.0


def test_lock_released_when_holder_connection_dies(pg_engine, pg_url):
    """Crash simulation: the server terminates the holder's session mid-sync."""
    holder_in, done = threading.Event(), threading.Event()
    crash_engine = build_engine(db_settings(pg_url), application_name="crashing-holder")

    def holder():
        try:
            with locks(crash_engine).hold(T, "SF-1"):
                holder_in.set()
                done.wait(10)
        except Exception:  # noqa: BLE001 - connection is gone; expected
            pass

    threads = start_threads([holder], name="crashing-holder")
    try:
        wait_event(holder_in, timeout=5, what="holder to acquire the lock")
        assert try_lock_elsewhere(pg_url, T, "SF-1") is False
        with pg_engine.begin() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                              "WHERE application_name = 'crashing-holder'"))
        wait_until(lambda: try_lock_elsewhere(pg_url, T, "SF-1"), timeout=5,
                   what="lock to be released after the holder session died")
    finally:
        done.set()
        join_all(threads, timeout=15, what="crashed holder thread to exit")
        crash_engine.dispose()


def test_database_unavailable_fails_closed(dead_engine):
    with pytest.raises(LockUnavailableError) as exc_info:
        with locks(dead_engine).hold(T, "SF-1"):
            pytest.fail("body must not run without the lock")
    assert exc_info.value.code == "database_unavailable"


def _run_lock_workers(pg_url, tmp_path, accounts, hold=0.3):
    barrier = str(tmp_path / f"go-{time.time_ns()}")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(len(accounts)) as pool:
        pending = [pool.apply_async(_workers.hold_lock, (pg_url, T, a, hold, barrier)) for a in accounts]
        time.sleep(1.5)  # let every process start and reach the barrier
        open(barrier, "w").close()
        return pool_results(pending, timeout=60, what=f"{len(accounts)} lock-holding processes")


def test_mutual_exclusion_across_processes(pg_url, pg_engine, tmp_path):
    results = _run_lock_workers(pg_url, tmp_path, ["SF-1"] * 4)
    assert len({r["pid"] for r in results}) == 4 and os.getpid() not in {r["pid"] for r in results}
    intervals = sorted((r["acquired"], r["released"]) for r in results)
    for (_, prev_end), (next_start, _) in zip(intervals, intervals[1:]):
        assert next_start >= prev_end, f"critical sections overlapped: {intervals}"
    # Strictly serialised: total time at least 4 x hold.
    assert intervals[-1][1] - intervals[0][0] >= 4 * 0.3 * 0.95


def test_different_accounts_run_in_parallel_across_processes(pg_url, pg_engine, tmp_path):
    results = _run_lock_workers(pg_url, tmp_path, ["SF-1", "SF-2", "SF-3", "SF-4"])
    intervals = sorted((r["acquired"], r["released"]) for r in results)
    # All four held their (different) locks at the same time.
    assert max(s for s, _ in intervals) < min(e for _, e in intervals)


# --- Milestone 6: session-level lock, no open transaction ------------------- #
def _lock_sessions(engine, app_name="integrationlab-test"):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT a.state, l.granted FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
            "WHERE l.locktype = 'advisory' AND a.application_name = :n"), {"n": app_name}).all()


def test_no_transaction_open_while_lock_held(pg_engine, pg_url):
    probe = build_engine(db_settings(pg_url), application_name="probe")
    try:
        with locks(pg_engine).hold(T, "SF-1"):
            sessions = _lock_sessions(probe)
        assert sessions == [("idle", True)]  # held, but not "idle in transaction"
    finally:
        probe.dispose()


def test_idle_session_backstop_set_while_held_and_reset_after(pg_engine):
    provider = PostgresAdvisorySyncLocks(pg_engine, lock_timeout_seconds=5, max_hold_seconds=123)
    with provider.hold(T, "SF-1"):
        pass
    # The pooled connection comes back with settings reset and no lock.
    with pg_engine.connect() as c:
        assert c.exec_driver_sql("SHOW idle_session_timeout").scalar() == "0"
        assert c.exec_driver_sql("SHOW lock_timeout").scalar() == "0"
        assert c.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                              "AND pid = pg_backend_pid()")).scalar() == 0


def test_idle_session_backstop_releases_lock_of_hung_holder(pg_engine, pg_url):
    """A holder that hangs past max_hold has its session ended by the server, freeing the lock."""
    provider = PostgresAdvisorySyncLocks(pg_engine, lock_timeout_seconds=5, max_hold_seconds=1)
    with provider.hold(T, "SF-1"):
        assert try_lock_elsewhere(pg_url, T, "SF-1") is False
        time.sleep(2.0)  # "hung" sync: lock session idle beyond idle_session_timeout
        assert try_lock_elsewhere(pg_url, T, "SF-1") is True  # server reclaimed it
    # Release found a dead session: no exception, connection invalidated, pool still healthy.
    with pg_engine.connect() as c:
        assert c.execute(text("SELECT 1")).scalar() == 1


def test_failed_unlock_invalidates_connection_instead_of_pooling_it(pg_engine, pg_url):
    """If the lock connection dies mid-sync, release can't run; the connection must not be reused."""
    admin = build_engine(db_settings(pg_url), application_name="admin")
    try:
        with locks(pg_engine).hold(T, "SF-1"):
            with admin.begin() as c:
                c.execute(text("SELECT pg_terminate_backend(l.pid) FROM pg_locks l JOIN pg_stat_activity a "
                               "USING (pid) WHERE l.locktype='advisory' "
                               "AND a.application_name='integrationlab-test'"))
        # No exception leaked to the caller; the lock is free; the pool hands out healthy connections.
        assert try_lock_elsewhere(pg_url, T, "SF-1") is True
        with pg_engine.connect() as c:
            assert c.execute(text("SELECT 1")).scalar() == 1
    finally:
        admin.dispose()


def test_lock_timeout_leaves_connection_clean(pg_engine):
    holder_in, release = threading.Event(), threading.Event()

    def holder():
        with locks(pg_engine).hold(T, "SF-1"):
            holder_in.set()
            release.wait(10)

    threads = start_threads([holder], name="holder")
    try:
        wait_event(holder_in, timeout=5, what="holder to acquire the lock")
        with pytest.raises(LockTimeoutError):
            with locks(pg_engine, timeout=0.2).hold(T, "SF-1"):
                pass
    finally:
        release.set()
        join_all(threads, timeout=15, what="lock holder to release")
    with pg_engine.connect() as c:
        assert c.exec_driver_sql("SHOW lock_timeout").scalar() == "0"
