"""Fixtures for tests against a real PostgreSQL database.

Safety rules:
- Only TEST_DATABASE_URL is used, never DATABASE_URL.
- The database name must end in ``_test``; otherwise the run aborts, because
  these fixtures TRUNCATE ``sync_records`` between tests.
- The test database is created if missing (CREATE DATABASE is additive).
- Migrations run forward (``upgrade head``) only.

If the database is unreachable the tests are skipped, unless
REQUIRE_POSTGRES=1, in which case they fail.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import DatabaseSettings
from app.db.engine import build_engine

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv(os.path.join(ROOT, ".env"))
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


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
    if not TEST_DATABASE_URL:
        _unavailable("TEST_DATABASE_URL not set")
    url = make_url(TEST_DATABASE_URL)
    if not (url.database or "").endswith("_test"):
        pytest.exit(f"Refusing to run: test database name {url.database!r} must end in '_test'",
                    returncode=2)

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


def db_settings(url: str, **overrides) -> DatabaseSettings:
    values = dict(url=url, pool_size=5, max_overflow=5, pool_timeout_seconds=5.0,
                  connect_timeout_seconds=2, lock_timeout_seconds=5.0)
    values.update(overrides)
    return DatabaseSettings(**values)


@pytest.fixture
def pg_engine(pg_url):
    engine = build_engine(db_settings(pg_url), application_name="integrationlab-test")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE sync_records RESTART IDENTITY"))
    yield engine
    engine.dispose()


#: An address where nothing listens, used to simulate the database being down.
DEAD_URL = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/unreachable_test"


@pytest.fixture
def dead_engine():
    engine = build_engine(db_settings(DEAD_URL, connect_timeout_seconds=1, pool_timeout_seconds=1))
    yield engine
    engine.dispose()
