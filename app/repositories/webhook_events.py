"""Durable webhook event inbox (PostgreSQL).

Lifecycle (Milestone 7)::

                 ┌──────────── retry (retryable, attempts < max): available_at = now + backoff ─┐
                 ▼                                                                             │
    pending ──claim──▶ processing ──complete──▶ succeeded | failed (permanent) | dead_letter ──┘
       ▲                    │                                     │               │
       │                    └─(lease expires: reclaimed, attempts+1; at max → dead_letter)
       └──────────── manual replay (failed | dead_letter → pending, fresh attempt budget) ◀┘
    ignored  (unsupported event types; terminal at ingestion)

Every completion and every lease expiry writes a row to ``webhook_event_attempts``
in the same statement as the state change (retry history).

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
from app.repositories.audit import AuditEntry, emit_log, insert_audit
from app.db.errors import translate_db_error


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
    #: "succeeded" | "failed" (permanent) | "pending" (retry scheduled) | "dead_letter"
    status: str
    sync_record_id: uuid.UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_retryable: bool | None = None
    #: Seconds until the retry becomes claimable (status "pending" only).
    retry_delay_seconds: float | None = None


_ATTEMPT_OUTCOME = {"succeeded": "succeeded", "pending": "retry_scheduled", "failed": "failed",
                    "dead_letter": "dead_letter"}


@dataclass(frozen=True)
class ReplayResult:
    found: bool
    replayed: bool
    previous_status: str | None


@dataclass(frozen=True)
class AttemptView:
    attempt: int
    worker_id: str | None
    started_at: datetime | None
    finished_at: datetime
    outcome: str
    error_code: str | None
    error_message: str | None
    retryable: bool | None
    next_attempt_at: datetime | None
    sync_record_id: uuid.UUID | None


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
    available_at: datetime | None = None
    dead_lettered_at: datetime | None = None
    replay_count: int = 0
    last_replayed_at: datetime | None = None
    last_replayed_by: str | None = None


_LEASE_LOST_ATTEMPT = """
    INSERT INTO webhook_event_attempts (event_uuid, tenant_id, attempt, worker_id, started_at, outcome,
                                        error_code, error_message, retryable)
    SELECT id, tenant_id, attempts, claimed_by, claimed_at, 'lease_expired', 'lease_expired',
           'worker did not complete before its lease expired (crash, kill or stall)', true
"""

_EXHAUST_SQL = text(f"""
    WITH ex AS (
        SELECT id, tenant_id, attempts, claimed_by, claimed_at FROM webhook_events
         WHERE status = 'processing' AND lease_expires_at < now() AND attempts >= :max_attempts
         FOR UPDATE SKIP LOCKED
    ), upd AS (
        UPDATE webhook_events e
           SET status = 'dead_letter', dead_lettered_at = now(), processed_at = now(),
               claim_token = NULL, lease_expires_at = NULL,
               last_error_code = 'worker_attempts_exhausted',
               last_error_message = 'event was claimed ' || e.attempts || ' times without completing '
                                    || '(worker crash or lease timeout)',
               last_error_retryable = false
          FROM ex WHERE e.id = ex.id
        RETURNING e.id
    )
    {_LEASE_LOST_ATTEMPT} FROM ex
""")

_CLAIM_SQL = text(f"""
    WITH picked AS (
        SELECT id, status, tenant_id, attempts, claimed_by, claimed_at FROM webhook_events
         WHERE (status = 'pending' AND available_at <= now())
            OR (status = 'processing' AND lease_expires_at < now() AND attempts < :max_attempts)
         ORDER BY available_at, received_at
         LIMIT :batch
         FOR UPDATE SKIP LOCKED
    ), lost AS (
        {_LEASE_LOST_ATTEMPT} FROM picked WHERE status = 'processing'
    )
    UPDATE webhook_events AS e
       SET status = 'processing', attempts = e.attempts + 1, claim_token = gen_random_uuid(),
           claimed_by = :worker, claimed_at = now(),
           lease_expires_at = now() + make_interval(secs => :lease_seconds)
      FROM picked AS c
     WHERE e.id = c.id
 RETURNING e.id, e.tenant_id, e.event_id, e.event_type, e.crm_account_id, e.attempts, e.claim_token
""")

# Outcome + attempt-history row in ONE statement, fenced by claim_token.
_COMPLETE_SQL = text("""
    WITH upd AS (
        UPDATE webhook_events
           SET status = CAST(:status AS varchar),
               available_at = CASE WHEN CAST(:status AS varchar) = 'pending'
                                   THEN now() + make_interval(secs => CAST(:delay AS double precision))
                                   ELSE available_at END,
               processed_at = CASE WHEN CAST(:status AS varchar) = 'pending' THEN NULL ELSE now() END,
               dead_lettered_at = CASE WHEN CAST(:status AS varchar) = 'dead_letter' THEN now() ELSE NULL END,
               claim_token = NULL, lease_expires_at = NULL,
               last_error_code = :code, last_error_message = :message, last_error_retryable = :retryable,
               sync_record_id = :sync_record_id
         WHERE id = :id AND claim_token = :token AND status = 'processing'
        RETURNING id, tenant_id, attempts, claimed_by, claimed_at, available_at
    )
    INSERT INTO webhook_event_attempts (event_uuid, tenant_id, attempt, worker_id, started_at, outcome,
                                        error_code, error_message, retryable, next_attempt_at, sync_record_id)
    SELECT id, tenant_id, attempts, claimed_by, claimed_at, :attempt_outcome, :code, :message, :retryable,
           CASE WHEN CAST(:status AS varchar) = 'pending' THEN available_at END, :sync_record_id
      FROM upd
    RETURNING event_uuid
""")

_REPLAY_SQL = text("""
    UPDATE webhook_events
       SET status = 'pending', available_at = now(), attempts = 0, claim_token = NULL,
           lease_expires_at = NULL, processed_at = NULL, dead_lettered_at = NULL,
           replay_count = replay_count + 1, last_replayed_at = now(), last_replayed_by = :actor
     WHERE id = :id AND tenant_id = :tenant AND status IN ('failed', 'dead_letter')
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
            raise translate_db_error(exc) from exc

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
            raise translate_db_error(exc) from exc
        return [ClaimedEvent(r.id, r.tenant_id, r.event_id, r.event_type, r.crm_account_id, r.attempts,
                             r.claim_token) for r in rows]

    def complete(self, event: ClaimedEvent, outcome: Outcome) -> bool:
        """Record the outcome. False means the lease was lost (another worker owns the event)."""
        if outcome.status not in _ATTEMPT_OUTCOME:
            raise ValueError(f"invalid outcome status {outcome.status!r}")
        params = {"id": event.id, "token": event.claim_token, "status": outcome.status,
                  "delay": outcome.retry_delay_seconds or 0.0,
                  "attempt_outcome": _ATTEMPT_OUTCOME[outcome.status],
                  "code": outcome.error_code,
                  "message": outcome.error_message[:ERROR_MESSAGE_MAX] if outcome.error_message else None,
                  "retryable": outcome.error_retryable, "sync_record_id": outcome.sync_record_id}
        try:
            with self._engine.begin() as conn:
                return conn.execute(_COMPLETE_SQL, params).first() is not None
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc

    # -- manual replay (state change + audit in one transaction) ----------- #
    def replay(self, tenant_id: str, event_uuid: uuid.UUID, *, audit: AuditEntry) -> ReplayResult:
        try:
            with self._engine.begin() as conn:
                current = conn.execute(text(
                    "SELECT status FROM webhook_events WHERE id = :id AND tenant_id = :t FOR UPDATE"),
                    {"id": event_uuid, "t": tenant_id}).scalar()
                if current is None:
                    return ReplayResult(False, False, None)
                replayed = conn.execute(_REPLAY_SQL, {"id": event_uuid, "tenant": tenant_id,
                                                      "actor": audit.actor[:255]}).rowcount == 1
                insert_audit(conn, AuditEntry(**{**audit.__dict__,
                                                 "outcome": "success" if replayed else "rejected",
                                                 "details": {**audit.details, "previous_status": current}}))
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
        emit_log(AuditEntry(**{**audit.__dict__, "outcome": "success" if replayed else "rejected",
                               "details": {**audit.details, "previous_status": current}}))
        return ReplayResult(True, replayed, current)

    def replay_bulk(self, tenant_id: str, *, statuses: tuple[str, ...], limit: int,
                    audit: AuditEntry) -> list[uuid.UUID]:
        """Replay up to ``limit`` of the tenant's events in ``statuses``; one audit row per event."""
        allowed = tuple(st for st in statuses if st in ("failed", "dead_letter"))
        if not allowed:
            return []
        try:
            with self._engine.begin() as conn:
                ids = [r.id for r in conn.execute(text(
                    "SELECT id, status FROM webhook_events WHERE tenant_id = :t AND status = ANY(:st) "
                    "ORDER BY received_at LIMIT :lim FOR UPDATE SKIP LOCKED"),
                    {"t": tenant_id, "st": list(allowed), "lim": max(1, min(limit, 100))})]
                done = []
                for eid in ids:
                    if conn.execute(_REPLAY_SQL, {"id": eid, "tenant": tenant_id,
                                                  "actor": audit.actor[:255]}).rowcount == 1:
                        insert_audit(conn, AuditEntry(**{**audit.__dict__, "outcome": "success",
                                                         "target_id": str(eid)}))
                        done.append(eid)
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
        emit_log(AuditEntry(**{**audit.__dict__, "outcome": "success",
                               "details": {**audit.details, "replayed": len(done)}}))
        return done

    def attempts(self, tenant_id: str, event_uuid: uuid.UUID) -> list[AttemptView]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT attempt, worker_id, started_at, finished_at, outcome, error_code, error_message, "
                    "retryable, next_attempt_at, sync_record_id FROM webhook_event_attempts "
                    "WHERE tenant_id = :t AND event_uuid = :id ORDER BY id"), {"t": tenant_id, "id": event_uuid})
                return [AttemptView(**r._mapping) for r in rows]
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc

    # -- reads (tenant-scoped) ------------------------------------------- #
    def get(self, tenant_id: str, event_id: uuid.UUID) -> WebhookEvent | None:
        stmt = select(WebhookEventRow).where(WebhookEventRow.tenant_id == tenant_id,
                                             WebhookEventRow.id == event_id)
        try:
            with self._sessions() as s:
                row = s.scalars(stmt).one_or_none()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
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
            raise translate_db_error(exc) from exc


def _view(r: WebhookEventRow) -> WebhookEvent:
    return WebhookEvent(r.id, r.tenant_id, r.event_id, r.event_type, r.crm_account_id, r.status, r.attempts,
                        r.occurred_at, r.received_at, r.processed_at, r.claimed_by, r.lease_expires_at,
                        r.last_error_code, r.last_error_message, r.last_error_retryable, r.sync_record_id,
                        r.available_at, r.dead_lettered_at, r.replay_count, r.last_replayed_at,
                        r.last_replayed_by)
