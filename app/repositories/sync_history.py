"""Synchronization history persistence (in-memory and PostgreSQL).

``SyncHistoryRepository`` is the contract the sync service depends on. The
in-memory implementation is for tests and local development without a
database; PostgresSyncHistoryRepository is the durable one. Every read requires a
``tenant_id`` so that tenant scoping can't be forgotten at a call site.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.db.errors import translate_db_error
from app.db.models import ERROR_MESSAGE_MAX, SyncRecordRow
from app.schemas.sync import SyncErrorDetail, SyncRecord, SyncStage, SyncStatus


class SyncHistoryRepository(Protocol):
    def add(self, record: SyncRecord) -> None: ...

    def get(self, tenant_id: str, record_id: UUID) -> SyncRecord | None: ...

    def list_recent(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        crm_account_id: str | None = None,
        status: SyncStatus | None = None,
    ) -> list[SyncRecord]: ...


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #
class PostgresSyncHistoryRepository:
    """Durable sync history. Each call runs in its own short transaction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def add(self, record: SyncRecord) -> None:
        row = _to_row(record)
        try:
            # session.begin(): COMMIT on success, ROLLBACK on any exception.
            with self._sessions.begin() as session:
                session.add(row)
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc

    def get(self, tenant_id: str, record_id: UUID) -> SyncRecord | None:
        stmt = select(SyncRecordRow).where(
            SyncRecordRow.tenant_id == tenant_id, SyncRecordRow.id == record_id)
        try:
            with self._sessions() as session:
                row = session.scalars(stmt).one_or_none()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
        return _from_row(row) if row else None

    def list_recent(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        crm_account_id: str | None = None,
        status: SyncStatus | None = None,
    ) -> list[SyncRecord]:
        stmt = select(SyncRecordRow).where(SyncRecordRow.tenant_id == tenant_id)
        if crm_account_id is not None:
            stmt = stmt.where(SyncRecordRow.crm_account_id == crm_account_id)
        if status is not None:
            stmt = stmt.where(SyncRecordRow.status == status.value)
        stmt = stmt.order_by(SyncRecordRow.seq.desc()).limit(max(1, min(limit, 500)))
        try:
            with self._sessions() as session:
                rows = session.scalars(stmt).all()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
        return [_from_row(r) for r in rows]


def _to_row(record: SyncRecord) -> SyncRecordRow:
    err = record.error
    return SyncRecordRow(
        id=record.id,
        tenant_id=record.tenant_id,
        crm_account_id=record.crm_account_id,
        status=record.status.value,
        stage=record.stage.value if record.stage else None,
        error_code=err.code if err else None,
        error_message=err.message[:ERROR_MESSAGE_MAX] if err else None,
        retryable=err.retryable if err else None,
        upstream_status=err.upstream_status if err else None,
        error_issues=list(err.issues) if err else [],
        destination_customer_id=record.destination_customer_id,
        fields_changed=list(record.fields_changed),
        mapping_version=record.mapping_version,
        source_last_modified=record.source_last_modified,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration_ms=record.duration_ms,
    )


def _from_row(row: SyncRecordRow) -> SyncRecord:
    error = None
    if row.error_code is not None:
        error = SyncErrorDetail(code=row.error_code, message=row.error_message or "",
                                retryable=bool(row.retryable), upstream_status=row.upstream_status,
                                issues=row.error_issues or [])
    return SyncRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        crm_account_id=row.crm_account_id,
        status=SyncStatus(row.status),
        stage=SyncStage(row.stage) if row.stage else None,
        error=error,
        destination_customer_id=row.destination_customer_id,
        fields_changed=list(row.fields_changed or []),
        mapping_version=row.mapping_version,
        source_last_modified=row.source_last_modified,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
    )
