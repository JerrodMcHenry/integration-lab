"""ORM models.

Only operational metadata is stored. There are no account payload columns:
names, revenue, owners and other CRM values never reach the database. Error
messages are produced by IntegrationLab's own clients and mapper, which
already exclude record values (enforced by tests).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SYNC_STATUSES = ("created", "updated", "unchanged", "skipped_stale", "failed")
SYNC_STAGES = ("acquire_lock", "fetch_source", "transform", "load_destination", "internal")
ERROR_MESSAGE_MAX = 1000


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class SyncRecordRow(Base):
    __tablename__ = "sync_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    #: Monotonic insertion order; gives stable "most recent first" ordering.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    tenant_id: Mapped[str] = mapped_column(String(63), nullable=False)
    crm_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    upstream_status: Mapped[int | None] = mapped_column(SmallInteger)
    #: [{"field","code","message"}], with field names and codes only.
    error_issues: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    destination_customer_id: Mapped[str | None] = mapped_column(String(64))
    fields_changed: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, server_default=text("'{}'::varchar[]"))
    mapping_version: Mapped[int | None] = mapped_column(Integer)
    source_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(r"tenant_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'", name="tenant_id_format"),
        CheckConstraint("length(crm_account_id) BETWEEN 1 AND 64", name="crm_account_id_length"),
        CheckConstraint(_in("status", SYNC_STATUSES), name="status_valid"),
        CheckConstraint(f"stage IS NULL OR {_in('stage', SYNC_STAGES)}", name="stage_valid"),
        # A failure always says where, what and whether to retry; a success carries no error.
        CheckConstraint(
            "(status = 'failed') = (stage IS NOT NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND retryable IS NOT NULL)",
            name="failure_fields_consistent",
        ),
        CheckConstraint(
            "status = 'failed' OR (stage IS NULL AND error_code IS NULL AND error_message IS NULL "
            "AND retryable IS NULL AND upstream_status IS NULL)",
            name="success_has_no_error",
        ),
        CheckConstraint(f"error_message IS NULL OR length(error_message) <= {ERROR_MESSAGE_MAX}",
                        name="error_message_length"),
        CheckConstraint("upstream_status IS NULL OR upstream_status BETWEEN 100 AND 599",
                        name="upstream_status_range"),
        CheckConstraint("jsonb_typeof(error_issues) = 'array'", name="error_issues_is_array"),
        CheckConstraint("mapping_version IS NULL OR mapping_version >= 1", name="mapping_version_positive"),
        CheckConstraint("finished_at >= started_at", name="finished_after_started"),
        CheckConstraint("duration_ms >= 0", name="duration_non_negative"),
        # Query paths: tenant history, per-account history, failures-only.
        Index("ix_sync_records_tenant_recent", "tenant_id", text("seq DESC")),
        Index("ix_sync_records_tenant_account_recent", "tenant_id", "crm_account_id", text("seq DESC")),
        Index("ix_sync_records_tenant_failed_recent", "tenant_id", text("seq DESC"),
              postgresql_where=text("status = 'failed'")),
    )
