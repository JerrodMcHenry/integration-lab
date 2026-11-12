"""Migrations, constraints and indexes against real PostgreSQL."""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Base

VALID = dict(
    tenant_id="meridian", crm_account_id="SF-1", status="created",
    started_at="2026-11-11T00:00:00Z", finished_at="2026-11-11T00:00:01Z", duration_ms=10,
)


def insert(engine, **overrides):
    row = {**VALID, "id": str(uuid.uuid4()), **overrides}
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO sync_records ({cols}) VALUES ({params})"), row)


def test_database_is_at_head(pg_engine, alembic_cfg):
    head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    with pg_engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == head


def test_models_match_migrations(pg_engine):
    """Migration and ORM model must not drift (equivalent of `alembic check`)."""
    with pg_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}),
                                Base.metadata)
    assert diff == []


def test_check_constraint_names_match_models(pg_engine):
    """Regression: 0001 applied the naming convention twice (ck_t_ck_t_x). 0003 renames them."""
    from sqlalchemy import CheckConstraint

    # SQLAlchemy stores the convention-applied name (ck_<table>_<name>) on the constraint.
    expected = {(t.name, str(c.name)) for t in Base.metadata.sorted_tables
                for c in t.constraints if isinstance(c, CheckConstraint)}
    assert all(name.startswith(f"ck_{t}_") and f"_ck_{t}_" not in name for t, name in expected)
    with pg_engine.connect() as conn:
        actual = set(conn.execute(text(
            "SELECT conrelid::regclass::text, conname FROM pg_constraint "
            "WHERE contype = 'c' AND connamespace = 'public'::regnamespace")).all())
    assert actual == expected


def test_no_account_payload_columns(pg_engine):
    columns = {c["name"] for c in inspect(pg_engine).get_columns("sync_records")}
    assert columns == {
        "id", "seq", "tenant_id", "crm_account_id", "status", "stage", "error_code",
        "error_message", "retryable", "upstream_status", "error_issues",
        "destination_customer_id", "fields_changed", "mapping_version", "source_last_modified",
        "started_at", "finished_at", "duration_ms", "recorded_at",
    }


def test_expected_indexes_exist(pg_engine):
    with pg_engine.connect() as conn:
        rows = dict(conn.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'sync_records'")).all())
    assert "ix_sync_records_tenant_recent" in rows
    assert "ix_sync_records_tenant_account_recent" in rows
    assert "WHERE" in rows["ix_sync_records_tenant_failed_recent"]


def test_valid_row_accepted(pg_engine):
    insert(pg_engine)
    insert(pg_engine, status="failed", stage="fetch_source", error_code="crm_timeout",
           error_message="timed out", retryable=True)


@pytest.mark.parametrize(
    "overrides, constraint",
    [
        ({"status": "exploded"}, "status_valid"),
        ({"stage": "bogus", "status": "failed", "error_code": "x", "error_message": "x",
          "retryable": False}, "stage_valid"),
        ({"status": "failed"}, "failure_fields_consistent"),
        ({"status": "failed", "stage": "transform", "error_code": "x", "error_message": "x"},
         "failure_fields_consistent"),
        ({"error_code": "x"}, "success_has_no_error"),
        ({"upstream_status": 503}, "success_has_no_error"),
        ({"tenant_id": "Bad Tenant"}, "tenant_id_format"),
        ({"tenant_id": "../x"}, "tenant_id_format"),
        ({"crm_account_id": ""}, "crm_account_id_length"),
        ({"finished_at": "2026-11-10T00:00:00Z"}, "finished_after_started"),
        ({"duration_ms": -1}, "duration_non_negative"),
        ({"mapping_version": 0}, "mapping_version_positive"),
        ({"status": "failed", "stage": "transform", "error_code": "x", "retryable": False,
          "error_message": "y" * 1001}, "error_message_length"),
    ],
)
def test_check_constraints_reject_invalid_rows(pg_engine, overrides, constraint):
    with pytest.raises(IntegrityError) as exc_info:
        insert(pg_engine, **overrides)
    assert constraint in str(exc_info.value.orig)
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sync_records")).scalar() == 0


def test_duplicate_primary_key_rejected(pg_engine):
    rid = str(uuid.uuid4())
    insert(pg_engine, id=rid)
    with pytest.raises(IntegrityError):
        insert(pg_engine, id=rid)


def test_seq_cannot_be_set_by_clients(pg_engine):
    with pytest.raises(Exception, match="GENERATED ALWAYS"):
        insert(pg_engine, seq=1)


def test_upgrade_is_idempotent(alembic_cfg):
    command.upgrade(alembic_cfg, "head")  # already at head: no-op, no error


def test_destructive_downgrade_refused_without_explicit_opt_in(pg_engine, alembic_cfg, monkeypatch):
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_DOWNGRADE", raising=False)
    insert(pg_engine)
    with pytest.raises(RuntimeError, match="ALLOW_DESTRUCTIVE_DOWNGRADE"):
        command.downgrade(alembic_cfg, "base")
    # Table and data are untouched.
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sync_records")).scalar() == 1
