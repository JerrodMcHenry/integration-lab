"""The test process can never reach the development database (fail-safe isolation)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url

from app import wiring
from tests.support.db import (
    UNCONFIGURED_DATABASE_URL,
    UnsafeTestDatabase,
    configured_test_database_url,
    validate_test_database_url,
)

DEV = "postgresql+psycopg://app:dev-pass@127.0.0.1:5434/integrationlab"


def test_process_database_url_is_the_unroutable_sentinel():
    # Compare booleans: a failing assertion must never print os.environ (it may hold secrets).
    from app.core.config import load_settings
    load_settings()  # calls load_dotenv(), which must not bring the dev URL back
    assert (os.environ["DATABASE_URL"] == UNCONFIGURED_DATABASE_URL) is True
    assert (os.environ.get("TEST_DATABASE_URL") == "") is True  # real value only via the accessor


def test_app_wiring_cannot_reach_the_development_database():
    url = wiring.engine().url
    assert (url.host, url.port, url.database) == ("127.0.0.1", 9, "no_database_configured")
    assert not url.database.endswith("_test")  # destructive fixtures would refuse it too


def test_configured_test_database_is_safe_when_present():
    try:
        url = make_url(configured_test_database_url())
    except UnsafeTestDatabase as exc:
        pytest.skip(f"no test database configured: {exc}")
    assert url.database.endswith("_test") and url.drivername.startswith("postgresql")


def test_valid_test_database_accepted():
    good = "postgresql+psycopg://app:pw@127.0.0.1:5434/integrationlab_test"
    assert validate_test_database_url(good, DEV) == good


@pytest.mark.parametrize("test_url, reason", [
    (None, "not set"),
    ("", "not set"),
    ("not a url at all", "not a valid database URL"),
    ("sqlite:///tmp/x_test", "must be PostgreSQL"),
    ("postgresql+psycopg://app:pw@127.0.0.1:5434/integrationlab", "must end in '_test'"),
    ("postgresql+psycopg://app:pw@127.0.0.1:5434/", "must end in '_test'"),
])
def test_unsafe_test_database_rejected(test_url, reason):
    with pytest.raises(UnsafeTestDatabase, match=reason):
        validate_test_database_url(test_url, DEV)


def test_test_database_that_is_the_dev_database_rejected():
    # A dev database that itself ends in _test must never be used for destructive fixtures.
    dev = "postgresql+psycopg://app:pw@127.0.0.1:5434/shared_test"
    with pytest.raises(UnsafeTestDatabase, match="development database"):
        validate_test_database_url("postgresql+psycopg://other:pw2@localhost:5434/shared_test", dev)


@pytest.mark.parametrize("test_url", [
    "postgresql+psycopg://app:S3cr3t-Pa55@127.0.0.1:5434/integrationlab",
    "sqlite://app:S3cr3t-Pa55@host/x",
    "postgresql+psycopg://app:S3cr3t-Pa55@127.0.0.1:5434/integrationlab_test",
])
def test_rejection_messages_never_contain_credentials(test_url):
    dev = "postgresql+psycopg://app:S3cr3t-Pa55@127.0.0.1:5434/integrationlab_test"
    with pytest.raises(UnsafeTestDatabase) as exc_info:
        validate_test_database_url(test_url, dev)
    assert "S3cr3t-Pa55" not in str(exc_info.value)
