"""Per-(tenant, CRM account) mutual exclusion for synchronization.

Two implementations share the ``SyncLockProvider`` contract:

* ``InProcessSyncLocks``: striped ``threading.Lock``s. Covers one process
  only. Used by unit tests and by local runs without a database.
* ``PostgresAdvisorySyncLocks``: PostgreSQL transaction-scoped advisory
  locks. Covers every process and host that uses the same database.

Lock lifecycle (PostgreSQL), revised in Milestone 6
---------------------------------------------------
Session-level advisory lock on an AUTOCOMMIT connection, so **no transaction
is open while the sync makes HTTP calls**. (M4 used a transaction-scoped lock,
which left the lock connection "idle in transaction" for the whole sync.)

1. Check out a pooled connection in AUTOCOMMIT mode (every statement commits
   on its own; nothing stays open).
2. ``SET lock_timeout`` caps how long we wait for another holder.
3. ``SELECT pg_advisory_lock(key)``: blocks until acquired or ``lock_timeout``
   fires (SQLSTATE 55P03 -> ``LockTimeoutError``).
4. ``SET idle_session_timeout`` (PostgreSQL >= 14): server-side backstop. If
   this session stays idle longer than the maximum legitimate sync duration
   (a hung process), the server ends it, which releases the lock.
5. The caller runs the whole sync (CRM read, mapping, CMS writes, history
   insert in its *own* short transaction). The lock connection is idle, not
   in a transaction.
6. ``SELECT pg_advisory_unlock(key)``, then ``RESET`` both settings and return
   the connection to the pool. **If unlock fails, returns false, or anything
   about the session is in doubt, the connection is invalidated (closed)
   instead of pooled.** Closing a session always releases its advisory locks,
   so a pooled connection can never carry a stale lock.
7. If the process crashes or the connection drops, the server ends the
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


class LockLostError(LockError):
    """Ownership check before a destination write found the lock no longer held."""

    code = "sync_lock_lost"


class LockHandle(Protocol):
    def verify(self) -> None:
        """Raise ``LockLostError`` unless this process still holds the lock."""


class _AlwaysHeld:
    def verify(self) -> None:
        return None


class SyncLockProvider(Protocol):
    def hold(self, tenant_id: str, crm_account_id: str):  # -> ContextManager[LockHandle]
        ...


def advisory_lock_key(tenant_id: str, crm_account_id: str) -> int:
    digest = hashlib.sha256(f"{tenant_id}\x00{crm_account_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class InProcessSyncLocks:
    """Bounded striped locks. Serialises within one process only."""

    def __init__(self, stripes: int = 64) -> None:
        self._locks = [threading.Lock() for _ in range(stripes)]

    @contextmanager
    def hold(self, tenant_id: str, crm_account_id: str) -> Iterator[LockHandle]:
        lock = self._locks[advisory_lock_key(tenant_id, crm_account_id) % len(self._locks)]
        with lock:
            yield _AlwaysHeld()


class PostgresAdvisorySyncLocks:
    def __init__(self, engine: Engine, *, lock_timeout_seconds: float,
                 max_hold_seconds: float = 300.0) -> None:
        self._engine = engine
        self._lock_timeout_ms = int(lock_timeout_seconds * 1000)
        #: Server-side idle_session_timeout backstop while the lock is held.
        self._idle_timeout_ms = int(max_hold_seconds * 1000)

    @contextmanager
    def hold(self, tenant_id: str, crm_account_id: str) -> Iterator[LockHandle]:
        key = advisory_lock_key(tenant_id, crm_account_id)
        try:
            conn = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        except (SQLAlchemyError, PoolTimeout) as exc:
            raise LockUnavailableError(f"cannot reach lock database: {type(exc).__name__}") from exc

        reusable = False
        try:
            # --- acquire (only these errors are translated into lock errors) ---
            try:
                # SET can't take bind parameters; values are ints we computed.
                conn.exec_driver_sql(f"SET lock_timeout = {self._lock_timeout_ms}")
                conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
                conn.exec_driver_sql(f"SET idle_session_timeout = {self._idle_timeout_ms}")
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "55P03":  # lock_not_available
                    reusable = _reset(conn)  # lock was not taken; session is clean
                    raise LockTimeoutError("another sync of this account is still running") from exc
                raise LockUnavailableError(f"lock acquisition failed: {type(exc).__name__}") from exc
            except SQLAlchemyError as exc:
                raise LockUnavailableError(f"lock acquisition failed: {type(exc).__name__}") from exc

            # --- critical section: caller's exceptions propagate unchanged ---
            try:
                yield _PgLockHandle(conn, key)
            finally:
                # --- release ---
                try:
                    released = conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key}).scalar()
                    reusable = bool(released) and _reset(conn)
                except SQLAlchemyError:
                    reusable = False  # session state unknown -> close it (releases the lock)
        finally:
            if not reusable:
                _quiet(conn.invalidate)
            _quiet(conn.close)


class _PgLockHandle:
    """Ownership check on the lock's own session (autocommit, no transaction)."""

    def __init__(self, conn, key: int) -> None:
        self._conn = conn
        self._classid = (key >> 32) & 0xFFFFFFFF
        self._objid = key & 0xFFFFFFFF

    def verify(self) -> None:
        try:
            held = self._conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND pid = pg_backend_pid() AND classid::bigint = :c AND objid::bigint = :o AND objsubid = 1)"),
                {"c": self._classid, "o": self._objid}).scalar()
        except SQLAlchemyError as exc:
            raise LockLostError(f"lock session unavailable: {type(exc).__name__}") from exc
        if not held:
            raise LockLostError("advisory lock no longer held by this session")


def _reset(conn) -> bool:
    try:
        conn.exec_driver_sql("RESET idle_session_timeout")
        conn.exec_driver_sql("RESET lock_timeout")
        return True
    except SQLAlchemyError:
        return False


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - cleanup must not mask the original error
        pass
