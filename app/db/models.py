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
    ForeignKey,
    PrimaryKeyConstraint,
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
    UniqueConstraint,
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


TENANT_ID_CHECK = r"~ '^[a-z0-9][a-z0-9-]{0,62}$'"
#: Secret references point at platform-managed secrets, never contain them, and are
#: namespaced per tenant: env:IL_<TENANT>__<NAME>. See app/services/secrets.py.
SECRET_REF_CHECK = r"~ '^env:IL_[A-Z0-9_]{1,120}$'"


class TenantRow(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(63), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"id {TENANT_ID_CHECK}", name="id_format"),
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 200", name="display_name_length"),
    )


class TenantIntegrationRow(Base):
    """Per-tenant connection settings for the CRM and the internal CMS.

    Managed by platform operators (seed/CLI). Not writable through the API,
    because base URLs (SSRF) and secret references (credential theft) are
    security-sensitive.
    """

    __tablename__ = "tenant_integrations"

    tenant_id: Mapped[str] = mapped_column(
        String(63), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True)
    crm_base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    crm_token_url: Mapped[str] = mapped_column(String(500), nullable=False)
    crm_client_id: Mapped[str] = mapped_column(String(200), nullable=False)
    crm_client_secret_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    cms_base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    cms_api_key_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    #: HMAC secret for inbound CRM webhooks; previous ref allows zero-downtime rotation.
    webhook_secret_ref: Mapped[str | None] = mapped_column(String(128))
    webhook_secret_previous_ref: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("crm_base_url ~ '^https?://'", name="crm_base_url_scheme"),
        CheckConstraint("crm_token_url ~ '^https?://'", name="crm_token_url_scheme"),
        CheckConstraint("cms_base_url ~ '^https?://'", name="cms_base_url_scheme"),
        CheckConstraint(f"crm_client_secret_ref {SECRET_REF_CHECK}", name="crm_secret_ref_format"),
        CheckConstraint(f"cms_api_key_ref {SECRET_REF_CHECK}", name="cms_key_ref_format"),
        CheckConstraint(f"webhook_secret_ref IS NULL OR webhook_secret_ref {SECRET_REF_CHECK}",
                        name="webhook_secret_ref_format"),
        CheckConstraint(f"webhook_secret_previous_ref IS NULL OR webhook_secret_previous_ref {SECRET_REF_CHECK}",
                        name="webhook_secret_previous_ref_format"),
    )


class TenantMappingConfigRow(Base):
    """Versioned, append-only field mapping configurations. Active = highest version."""

    __tablename__ = "tenant_mapping_configs"

    tenant_id: Mapped[str] = mapped_column(
        String(63), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "version", name="pk_tenant_mapping_configs"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="config_is_object"),
        # A stored config can only ever describe its own tenant.
        CheckConstraint("config->>'tenant_id' = tenant_id", name="config_tenant_matches"),
        CheckConstraint("(config->>'version')::int = version", name="config_version_matches"),
    )


class SyncRecordRow(Base):
    __tablename__ = "sync_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    #: Monotonic insertion order; gives stable "most recent first" ordering.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    tenant_id: Mapped[str] = mapped_column(
        String(63), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
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


WEBHOOK_EVENT_STATUSES = ("pending", "processing", "succeeded", "failed", "ignored")


class WebhookEventRow(Base):
    """Inbound CRM events: an inbox persisted before acknowledgment, processed by workers.

    ``payload`` holds only the allowlisted event envelope (ids, type, timestamps).
    Never headers, signatures, secrets or account field values.
    """

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(63), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    crm_account_id: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Earliest time a worker may claim the event (Milestone 7 backoff sets this).
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    last_error_retryable: Mapped[bool | None] = mapped_column(Boolean)
    sync_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_records.id", ondelete="RESTRICT"))

    __table_args__ = (
        # Durable, tenant-scoped idempotency: one row per provider event per tenant.
        UniqueConstraint("tenant_id", "source", "event_id"),
        CheckConstraint("source IN ('crm')", name="source_valid"),
        CheckConstraint("length(event_id) BETWEEN 1 AND 128", name="event_id_length"),
        CheckConstraint(_in("status", WEBHOOK_EVENT_STATUSES), name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="payload_sha256_format"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
        CheckConstraint("(status = 'processing') = (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
                        name="lease_iff_processing"),
        CheckConstraint("status NOT IN ('succeeded', 'failed', 'ignored') OR processed_at IS NOT NULL",
                        name="terminal_has_processed_at"),
        CheckConstraint("status <> 'failed' OR last_error_code IS NOT NULL", name="failed_has_error"),
        CheckConstraint(f"last_error_message IS NULL OR length(last_error_message) <= {ERROR_MESSAGE_MAX}",
                        name="error_message_length"),
        Index("ix_webhook_events_claimable", "available_at", "received_at",
              postgresql_where=text("status = 'pending'")),
        Index("ix_webhook_events_expired_leases", "lease_expires_at",
              postgresql_where=text("status = 'processing'")),
        Index("ix_webhook_events_tenant_recent", "tenant_id", text("received_at DESC")),
    )
