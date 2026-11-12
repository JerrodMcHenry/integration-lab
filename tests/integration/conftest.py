"""Fixtures for tests against a real PostgreSQL database.

Safety rules (enforced in ``tests/support/db.py`` and below):
- Only ``TEST_DATABASE_URL`` is used. The development ``DATABASE_URL`` is hidden from
  the test process entirely (see ``isolate_database_environment``).
- The test database must be PostgreSQL, named ``*_test``, and not the development
  database; otherwise the whole run aborts before any fixture can modify data.
- Before truncating, ``pg_engine`` re-checks ``current_database()`` on the live connection.
- The test database is created if missing (CREATE DATABASE is additive).
- Migrations run forward (``upgrade head``) only.

If the database is unreachable the tests are skipped, unless REQUIRE_POSTGRES=1,
in which case they fail.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db.engine import build_engine
from tests.support.data import ROOT
from tests.support.db import DEAD_URL, TEST_TENANTS, UnsafeTestDatabase, db_settings, configured_test_database_url
from tests.support.tenants import World
from tests.support.webhooks import build_service, seed_tenants



def pytest_collection_modifyitems(config, items):
    here = os.path.dirname(os.path.abspath(__file__))
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.postgres)


def _unavailable(reason: str):
    if os.getenv("REQUIRE_POSTGRES") == "1":
        pytest.fail(f"PostgreSQL required but unavailable: {reason}", pytrace=False)
    pytest.skip(f"PostgreSQL unavailable: {reason}")


@pytest.fixture(scope="session")
def pg_url() -> str:
    try:
        url = make_url(configured_test_database_url())
    except UnsafeTestDatabase as exc:
        if "not set" in str(exc):
            _unavailable(str(exc))
        pytest.exit(f"Refusing to run database tests: {exc}", returncode=2)

    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT",
                          connect_args={"connect_timeout": 3})
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                                  {"n": url.database}).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except Exception as exc:  # noqa: BLE001
        _unavailable(f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
    finally:
        admin.dispose()

    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def alembic_cfg(pg_url) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return cfg



@pytest.fixture
def pg_engine(pg_url):
    engine = build_engine(db_settings(pg_url), application_name="integrationlab-test")
    with engine.begin() as conn:
        # Second, independent check on the live connection before anything destructive.
        actual = conn.execute(text("SELECT current_database()")).scalar()
        if not actual.endswith("_test"):
            pytest.exit(f"Refusing to TRUNCATE: connected to {actual!r}, not a *_test database", returncode=2)
        # audit_log is append-only (TRUNCATE blocked by trigger): disable the trigger for this
        # test-database reset only, inside the same transaction.
        # Bounded: a connection leaked by an earlier test fails this loudly instead of hanging.
        conn.execute(text("SET LOCAL lock_timeout = '30s'"))
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_truncate"))
        conn.execute(text("TRUNCATE audit_log, worker_heartbeats, webhook_event_attempts, webhook_events, "
                          "sync_records, tenant_mapping_configs, tenant_integrations, tenants RESTART IDENTITY"))
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate"))
        conn.execute(text("INSERT INTO tenants (id, display_name) VALUES " + ", ".join(
            f"('{t}', '{t}')" for t in TEST_TENANTS)))
    yield engine
    engine.dispose()


#: An address where nothing listens, used to simulate the database being down.


@pytest.fixture
def dead_engine():
    engine = build_engine(db_settings(DEAD_URL, connect_timeout_seconds=1, pool_timeout_seconds=1))
    yield engine
    engine.dispose()


@pytest.fixture
def world(pg_engine):
    seed_tenants(pg_engine)
    return World()  # in-process CRM/CMS per tenant, routed by base URL


@pytest.fixture
def service(pg_engine, world):
    return build_service(pg_engine, http_client_factory=world.route)
