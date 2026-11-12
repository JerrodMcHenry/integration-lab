"""Synchronization result and history models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SyncStatus(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED_STALE = "skipped_stale"
    FAILED = "failed"

    @property
    def succeeded(self) -> bool:
        return self is not SyncStatus.FAILED


class SyncStage(str, Enum):
    ACQUIRE_LOCK = "acquire_lock"
    FETCH_SOURCE = "fetch_source"
    TRANSFORM = "transform"
    LOAD_DESTINATION = "load_destination"
    INTERNAL = "internal"


class SyncErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    upstream_status: int | None = None
    #: Per-field problems for mapping failures (field names only, never values).
    issues: list[dict[str, str]] = Field(default_factory=list)


class SyncRecord(BaseModel):
    """One synchronization attempt. Contains identifiers and field *names* only, no account data."""

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    crm_account_id: str
    status: SyncStatus
    stage: SyncStage | None = None
    error: SyncErrorDetail | None = None
    destination_customer_id: str | None = None
    fields_changed: list[str] = Field(default_factory=list)
    mapping_version: int | None = None
    source_last_modified: datetime | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: int

    @property
    def succeeded(self) -> bool:
        return self.status.succeeded
