"""Structured audit records for sensitive operational actions.

Rows are append-only (enforced by a database trigger in PostgreSQL). Each
record is also emitted as a single JSON log line on the ``integrationlab.audit``
logger. ``details`` must never contain secrets, tokens or customer payloads.
Callers pass identifiers, counts, reasons and status transitions only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, insert, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import AuditLogRow
from app.db.errors import translate_db_error

audit_logger = logging.getLogger("integrationlab.audit")
_DETAIL_KEYS_FORBIDDEN = ("secret", "token", "password", "authorization", "signature", "api_key")


@dataclass(frozen=True)
class AuditEntry:
    actor: str
    action: str
    outcome: str  # success | denied | rejected | error
    tenant_id: str | None = None
    actor_client_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        bad = [k for k in self.details if any(f in k.lower() for f in _DETAIL_KEYS_FORBIDDEN)]
        if bad:
            raise ValueError(f"audit details must not contain sensitive keys: {bad}")


def emit_log(entry: AuditEntry) -> None:
    payload = {k: v for k, v in asdict(entry).items() if v not in (None, {})}
    payload["occurred_at"] = (entry.occurred_at or datetime.now(timezone.utc)).isoformat()
    audit_logger.info(json.dumps(payload, sort_keys=True, default=str))


def insert_audit(conn: Connection, entry: AuditEntry) -> None:
    """Insert inside the caller's transaction (so the action and its audit commit together)."""
    conn.execute(insert(AuditLogRow).values(
        tenant_id=entry.tenant_id, actor=entry.actor[:255], actor_client_id=(entry.actor_client_id or None),
        action=entry.action, target_type=entry.target_type, target_id=entry.target_id,
        outcome=entry.outcome, details=entry.details))


class AuditRepository(Protocol):
    def record(self, entry: AuditEntry) -> None: ...
    def list_recent(self, tenant_id: str, *, limit: int = 100, action: str | None = None) -> list[AuditEntry]: ...


class PostgresAuditRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, entry: AuditEntry) -> None:
        try:
            with self._engine.begin() as conn:
                insert_audit(conn, entry)
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
        emit_log(entry)

    def list_recent(self, tenant_id: str, *, limit: int = 100, action: str | None = None) -> list[AuditEntry]:
        stmt = select(AuditLogRow).where(AuditLogRow.tenant_id == tenant_id)
        if action:
            stmt = stmt.where(AuditLogRow.action == action)
        stmt = stmt.order_by(AuditLogRow.id.desc()).limit(max(1, min(limit, 500)))
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).all()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
        return [AuditEntry(actor=r.actor, action=r.action, outcome=r.outcome, tenant_id=r.tenant_id,
                           actor_client_id=r.actor_client_id, target_type=r.target_type, target_id=r.target_id,
                           details=r.details, occurred_at=r.occurred_at) for r in rows]
