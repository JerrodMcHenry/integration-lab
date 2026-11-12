"""Per-(tenant, CRM account) mutual exclusion for synchronization.

Two implementations share the ``SyncLockProvider`` contract:

* ``InProcessSyncLocks``: striped ``threading.Lock``s. Covers one process
  only. Used by unit tests and by local runs without a database.
* ``PostgresAdvisorySyncLocks``: PostgreSQL transaction-scoped advisory
  locks. Covers every process and host that uses the same database.

Lock lifecycle (PostgreSQL)
---------------------------
1. Check out a dedicated pooled connection and ``BEGIN``.
2. ``SET LOCAL lock_timeout`` caps how long we wait for another holder, and
   ``SET LOCAL idle_in_transaction_session_timeout`` is a backstop that
   lets the server reclaim the lock if this process hangs.
3. ``SELECT pg_advisory_xact_lock(key)``: blocks until acquired or
   ``lock_timeout`` fires (SQLSTATE 55P03 -> ``LockTimeoutError``).
4. The caller runs the whole sync (CRM read, mapping, CMS writes, history
   insert) while the transaction stays open. No rows are written on this
   connection; it exists only to hold the lock.
5. On exit: ``COMMIT`` on success, ``ROLLBACK`` on exception. Either way
   PostgreSQL releases transaction-scoped advisory locks automatically, so
   there's no unlock call that can be forgotten or fail halfway.
6. If the process crashes or the connection drops, the server ends the
   session and releases the lock.

The key is the first 8 bytes of ``sha256(tenant_id NUL crm_account_id)``,
read as a signed 64-bit integer. A hash collision only makes two unrelated
accounts serialise (a performance issue, never a correctness one).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout


class LockError(RuntimeError):
    code = "lock_error"
    retryable = True


class LockTimeoutError(LockError):
    """Another worker held the lock for longer than the configured timeout."""

    code = "sync_lock_timeout"


class LockUnavailableError(LockError):
    """The lock service (database) could not be reached. Sync fails closed."""

    code = "database_unavailable"


class SyncLockProvider(Protocol):
    def hold(self, tenant_id: str, crm_account_id: str):  # -> ContextManager[None]
        ...


def advisory_lock_key(tenant_id: str, crm_account_id: str) -> int:
    digest = hashlib.sha256(f"{tenant_id}\x00{crm_account_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class InProcessSyncLocks:
    """Bounded striped locks. Serialises within one process only."""

    def __init__(self, stripes: int = 64) -> None:
        self._locks = [threading.Lock() for _ in range(stripes)]

    @contextmanager
    def hold(self, tenant_id: str, crm_account_id: str) -> Iterator[None]:
        lock = self._locks[advisory_lock_key(tenant_id, crm_account_id) % len(self._locks)]
        with lock:
            yield


class PostgresAdvisorySyncLocks:
    def __init__(self, engine: Engine, *, lock_timeout_seconds: float,
                 max_hold_seconds: float = 300.0) -> None:
        self._engine = engine
        self._lock_timeout_ms = int(lock_timeout_seconds * 1000)
        self._idle_timeout_ms = int(max_hold_seconds * 1000)

    @contextmanager
    def hold(self, tenant_id: str, crm_account_id: str) -> Iterator[None]:
        key = advisory_lock_key(tenant_id, crm_account_id)
        try:
            conn = self._engine.connect()
        except (SQLAlchemyError, PoolTimeout) as exc:
            raise LockUnavailableError(f"cannot reach lock database: {type(exc).__name__}") from exc

        try:
            # --- acquire (only these errors are translated into lock errors) ---
            try:
                txn = conn.begin()
                # SET LOCAL can't take bind parameters; values are ints we computed.
                conn.execute(text(f"SET LOCAL lock_timeout = {self._lock_timeout_ms}"))
                conn.execute(text(
                    f"SET LOCAL idle_in_transaction_session_timeout = {self._idle_timeout_ms}"))
                conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            except DBAPIError as exc:
                _quiet(conn.rollback)
                if getattr(exc.orig, "sqlstate", None) == "55P03":  # lock_not_available
                    raise LockTimeoutError("another sync of this account is still running") from exc
                raise LockUnavailableError(f"lock acquisition failed: {type(exc).__name__}") from exc
            except SQLAlchemyError as exc:
                _quiet(conn.rollback)
                raise LockUnavailableError(f"lock acquisition failed: {type(exc).__name__}") from exc

            # --- critical section: caller's exceptions propagate unchanged ---
            try:
                yield
            except BaseException:
                _quiet(txn.rollback)  # releases the lock
                raise
            # --- release ---
            try:
                txn.commit()  # releases the lock
            except SQLAlchemyError:
                # The work already finished. If the connection died, the server
                # released the lock together with the session, so nothing leaks.
                _quiet(txn.rollback)
        finally:
            _quiet(conn.close)


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - cleanup must not mask the original error
        pass
