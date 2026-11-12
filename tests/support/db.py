"""PostgreSQL test helpers: test-database safety, settings, record factories, lock probes.

Test-database safety (see ``isolate_database_environment``):
- The test process never sees the development ``DATABASE_URL``. It's replaced
  by an unroutable sentinel before ``app`` is imported, so code that reaches real
  wiring without an explicit test override fails loudly instead of using the dev DB.
- Database-backed tests use only ``TEST_DATABASE_URL``, which must be PostgreSQL,
  have a name ending in ``_test``, and not be the development database.
- Messages never include passwords.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from sqlalchemy import text

from app.core.config import DatabaseSettings
from app.db.engine import build_engine
from app.schemas.sync import SyncRecord, SyncStatus
from app.services.locks import advisory_lock_key

#: Tenant rows present before every integration test (sync_records has a FK to tenants).
TEST_TENANTS = ("meridian", "harbor", "other", "other-tenant")

#: What the test process sees as DATABASE_URL: nothing listens on port 9, and the name is not a
#: ``_test`` database, so it can neither reach the dev database nor be truncated by fixtures.
UNCONFIGURED_DATABASE_URL = "postgresql+psycopg://tests-must-not-use-app-wiring@127.0.0.1:9/no_database_configured"

#: An address where nothing listens, used to simulate the database being down.
DEAD_URL = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/unreachable_test"


def db_settings(url: str, **overrides) -> DatabaseSettings:
    values = dict(url=url, pool_size=5, max_overflow=5, pool_timeout_seconds=5.0,
                  connect_timeout_seconds=2, lock_timeout_seconds=5.0)
    values.update(overrides)
    return DatabaseSettings(**values)


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def rec(account="SF-1", status=SyncStatus.UPDATED, tenant="meridian", **kw) -> SyncRecord:
    values = dict(tenant_id=tenant, crm_account_id=account, status=status, started_at=NOW,
                  finished_at=NOW + timedelta(milliseconds=5), duration_ms=5, mapping_version=1)
    values.update(kw)
    return SyncRecord(**values)


def try_lock_elsewhere(url: str, tenant: str, account: str) -> bool:
    """Probe from an independent engine: can someone else take this lock right now?"""
    engine = build_engine(db_settings(url), application_name="probe")
    try:
        with engine.begin() as conn:
            return conn.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                {"k": advisory_lock_key(tenant, account)}).scalar()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# Test-database isolation
# --------------------------------------------------------------------------- #
class UnsafeTestDatabase(RuntimeError):
    """TEST_DATABASE_URL is missing safeguards; destructive fixtures must not run."""


_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_state: dict[str, str | None] = {"test_url": None, "problem": None}


def _describe(url) -> str:
    return f"{url.database!r} on {url.host}:{url.port}"


def validate_test_database_url(test_url: str | None, dev_url: str | None) -> str:
    """Return ``test_url`` if it's safe for destructive fixtures, else raise ``UnsafeTestDatabase``."""
    if not test_url:
        raise UnsafeTestDatabase("TEST_DATABASE_URL is not set")
    try:
        test = make_url(test_url)
    except Exception as exc:  # noqa: BLE001 - never echo the URL (it may contain a password)
        raise UnsafeTestDatabase("TEST_DATABASE_URL is not a valid database URL") from exc
    if not test.drivername.startswith("postgresql"):
        raise UnsafeTestDatabase(f"TEST_DATABASE_URL must be PostgreSQL, got driver {test.drivername!r}")
    if not (test.database or "").endswith("_test"):
        raise UnsafeTestDatabase(f"test database name {test.database!r} must end in '_test'")
    if dev_url:
        try:
            dev = make_url(dev_url)
        except Exception:  # noqa: BLE001
            dev = None
        if dev is not None and dev.database == test.database:
            raise UnsafeTestDatabase(f"TEST_DATABASE_URL points at the development database {_describe(test)}")
    return test_url


def isolate_database_environment() -> None:
    """Call before importing ``app``. Hides the dev DATABASE_URL and records the validated test URL."""
    file_values = dotenv_values(_ENV_FILE) if _ENV_FILE.exists() else {}
    dev_url = os.environ.get("DATABASE_URL") or file_values.get("DATABASE_URL")
    test_url = os.environ.get("TEST_DATABASE_URL") or file_values.get("TEST_DATABASE_URL")
    if _state["test_url"] is not None:  # already isolated in this process
        return
    try:
        _state["test_url"] = validate_test_database_url(test_url, dev_url)
        _state["problem"] = None
    except UnsafeTestDatabase as exc:
        _state["test_url"], _state["problem"] = None, str(exc)
    os.environ["DATABASE_URL"] = UNCONFIGURED_DATABASE_URL
    # Empty, not removed: app settings call load_dotenv(), which re-adds missing keys from .env
    # but never overrides set ones. The real value is reachable only via configured_test_database_url().
    os.environ["TEST_DATABASE_URL"] = ""


def configured_test_database_url() -> str:
    """The validated test database URL, or ``UnsafeTestDatabase`` explaining why there isn't one."""
    if _state["test_url"] is None:
        raise UnsafeTestDatabase(_state["problem"] or "isolate_database_environment() was not called")
    return _state["test_url"]

