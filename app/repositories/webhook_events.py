"""Durable webhook event inbox (PostgreSQL).

Lifecycle::

    pending ──claim──▶ processing ──complete──▶ succeeded | failed
       ▲                    │
       └──(lease expires: reclaimed, attempts + 1)
    ignored  (unsupported event types; terminal at ingestion)

* **Ingest** commits the row *before* the HTTP response is sent.
* **Claim** is one short transaction: ``SELECT ... FOR UPDATE SKIP LOCKED``
  picks rows no other worker is claiming, then an ``UPDATE`` sets
  ``status=processing``, a fresh ``claim_token``, and ``lease_expires_at``.
  It commits immediately: **no transaction is held while the event is processed.**
* **Complete** is a second short transaction, fenced by ``claim_token``. If
  the lease expired and another worker reclaimed the event, the stale
  worker's update matches zero rows and is discarded.
* **Crash recovery:** a ``processing`` row whose lease has expired is claimable
  again. ``attempts`` counts claims. Once it reaches ``max_attempts``, the row
  is marked ``failed`` (``worker_attempts_exhausted``) instead of being
  reclaimed forever.

Milestone 7 builds retries on ``available_at`` (backoff) and adds dead-letter
and replay states. Today a processing failure is a terminal ``failed`` with
``last_error_retryable`` recorded.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.db.models import ERROR_MESSAGE_MAX, WebhookEventRow
from app.repositories.sync_history import _translate


@dataclass(frozen=True)
class NewEvent:
    tenant_id: str
    source: str
    event_id: str
    event_type: str
    crm_account_id: str | None
    occurred_at: datetime | None
    signed_at: datetime
    payload: dict[str, Any]
    payload_sha256: str
    ignored: bool = False


@dataclass(frozen=True)
class IngestResult:
    id: uuid.UUID
    created: bool
    status: str
    #: False when an existing event_id arrived with a different body.
    same_content: bool


@dataclass(frozen=True)
class ClaimedEvent:
    id: uuid.UUID
    tenant_id: str
    event_id: str
    event_type: str
    crm_account_id: str | None
    attempts: int
    claim_token: uuid.UUID


@dataclass(frozen=True)
class Outcome:
    status: str  # "succeeded" | "failed"
    sync_record_id: uuid.UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_retryable: bool | None = None


@dataclass(frozen=True)
class WebhookEvent:
    id: uuid.UUID
    tenant_id: str
    event_id: str
    event_type: str
    crm_account_id: str | None
    status: str
    attempts: int
    occurred_at: datetime | None
    received_at: datetime
    processed_at: datetime | None
    claimed_by: str | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    last_error_retryable: bool | None
    sync_record_id: uuid.UUID | None


_EXHAUST_SQL = text("""
    UPDATE webhook_events
       SET status = 'failed', processed_at = now(), claim_token = NULL, lease_expires_at = NULL,
           last_error_code = 'worker_attempts_exhausted',
           last_error_message = 'event was claimed ' || attempts || ' times without completing '
                                || '(worker crash or lease timeout)',
           last_error_retryable = false
     WHERE id IN (SELECT id FROM webhook_events
                   WHERE status = 'processing' AND lease_expires_at < now() AND attempts >= :max_attempts
                   FOR UPDATE SKIP LOCKED)
""")

_CLAIM_SQL = text("""
    UPDATE webhook_events AS e
       SET status = 'processing', attempts = e.attempts + 1, claim_token = gen_random_uuid(),
           claimed_by = :worker, claimed_at = now(),
           lease_expires_at = now() + make_interval(secs => :lease_seconds)
      FROM (SELECT id FROM webhook_events
             WHERE (status = 'pending' AND available_at <= now())
                OR (status = 'processing' AND lease_expires_at < now() AND attempts < :max_attempts)
             ORDER BY available_at, received_at
             LIMIT :batch
             FOR UPDATE SKIP LOCKED) AS c
     WHERE e.id = c.id
 RETURNING e.id, e.tenant_id, e.event_id, e.event_type, e.crm_account_id, e.attempts, e.claim_token
""")

_COMPLETE_SQL = text("""
    UPDATE webhook_events
       SET status = :status, processed_at = now(), claim_token = NULL, lease_expires_at = NULL,
           last_error_code = :code, last_error_message = :message, last_error_retryable = :retryable,
           sync_record_id = :sync_record_id
     WHERE id = :id AND claim_token = :token AND status = 'processing'
""")


class PostgresWebhookEventRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    # -- ingestion ------------------------------------------------------- #
    def ingest(self, ev: NewEvent) -> IngestResult:
        values = dict(id=uuid.uuid4(), tenant_id=ev.tenant_id, source=ev.source, event_id=ev.event_id,
                      event_type=ev.event_type, crm_account_id=ev.crm_account_id, occurred_at=ev.occurred_at,
                      signed_at=ev.signed_at, payload=ev.payload, payload_sha256=ev.payload_sha256)
        if ev.ignored:
            values.update(status="ignored", processed_at=func.now())
        stmt = (pg_insert(WebhookEventRow).values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "source", "event_id"])
                .returning(WebhookEventRow.id, WebhookEventRow.status))
        try:
            with self._engine.begin() as conn:  # COMMIT before the caller acknowledges
                row = conn.execute(stmt).first()
                if row is not None:
                    return IngestResult(row.id, True, row.status, True)
                existing = conn.execute(
                    select(WebhookEventRow.id, WebhookEventRow.status, WebhookEventRow.payload_sha256)
                    .where(WebhookEventRow.tenant_id == ev.tenant_id, WebhookEventRow.source == ev.source,
                           WebhookEventRow.event_id == ev.event_id)).one()
                return IngestResult(existing.id, False, existing.status,
                                    existing.payload_sha256 == ev.payload_sha256)
        except SQLAlchemyError as exc:
            raise _translate(exc) from exc

    # -- worker ---------------------------------------------------------- #
    def claim(self, worker_id: str, *, batch: int, lease_seconds: float,
              max_attempts: int) -> list[ClaimedEvent]:
        try:
            with self._engine.begin() as conn:  # short transaction; committed before processing
                conn.execute(_EXHAUST_SQL, {"max_attempts": max_attempts})
                rows = conn.execute(_CLAIM_SQL, {"worker": worker_id[:128], "batch": batch,
                                                 "lease_seconds": lease_seconds,
                                                 "max_attempts": max_attempts}).all()
        except SQLAlchemyError as exc:
            raise _translate(exc) from exc
        return [ClaimedEvent(r.id, r.tenant_id, r.event_id, r.event_type, r.crm_account_id, r.attempts,
                             r.claim_token) for r in rows]

    def complete(self, event: ClaimedEvent, outcome: Outcome) -> bool:
        """Record the outcome. False means the lease was lost (another worker owns the event)."""
        params = {"id": event.id, "token": event.claim_token, "status": outcome.status,
                  "code": outcome.error_code,
                  "message": outcome.error_message[:ERROR_MESSAGE_MAX] if outcome.error_message else None,
                  "retryable": outcome.error_retryable, "sync_record_id": outcome.sync_record_id}
        try:
            with self._engine.begin() as conn:
                return conn.execute(_COMPLETE_SQL, params).rowcount == 1
        except SQLAlchemyError as exc:
            raise _translate(exc) from exc

    # -- reads (tenant-scoped) ------------------------------------------- #
    def get(self, tenant_id: str, event_id: uuid.UUID) -> WebhookEvent | None:
        stmt = select(WebhookEventRow).where(WebhookEventRow.tenant_id == tenant_id,
                                             WebhookEventRow.id == event_id)
        try:
            with self._sessions() as s:
                row = s.scalars(stmt).one_or_none()
        except SQLAlchemyError as exc:
            raise _translate(exc) from exc
        return _view(row) if row else None

    def list_recent(self, tenant_id: str, *, limit: int = 50, status: str | None = None) -> list[WebhookEvent]:
        stmt = select(WebhookEventRow).where(WebhookEventRow.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(WebhookEventRow.status == status)
        stmt = stmt.order_by(WebhookEventRow.received_at.desc()).limit(max(1, min(limit, 500)))
        try:
            with self._sessions() as s:
                return [_view(r) for r in s.scalars(stmt).all()]
        except SQLAlchemyError as exc:
            raise _translate(exc) from exc


def _view(r: WebhookEventRow) -> WebhookEvent:
    return WebhookEvent(r.id, r.tenant_id, r.event_id, r.event_type, r.crm_account_id, r.status, r.attempts,
                        r.occurred_at, r.received_at, r.processed_at, r.claimed_by, r.lease_expires_at,
                        r.last_error_code, r.last_error_message, r.last_error_retryable, r.sync_record_id)
