"""Account synchronization: CRM -> internal customer management system.

Orchestration only. HTTP details live in the integration clients, field rules
live in the mapper, and storage lives behind ``SyncHistoryRepository``.

Every call to :meth:`AccountSyncService.sync_account` produces exactly one
``SyncRecord`` that is persisted to history and returned, whether it succeeded
or failed. Expected failures never raise, and none are silent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from app.integrations.crm_client import CRMClient
from app.integrations.errors import IntegrationError
from app.integrations.internal_client import InternalConflictError, InternalSystemClient
from app.mapping.mapper import FieldMapper, MappingConfigError, MappingError
from app.repositories.sync_history import SyncHistoryRepository
from app.schemas.internal_customer import (
    CRM_OWNED_FIELDS,
    DESTINATION_OWNED_FIELDS,
    CustomerSyncPayload,
)
from app.schemas.sync import SyncErrorDetail, SyncRecord, SyncStage, SyncStatus
from app.services.locks import InProcessSyncLocks, LockError, LockHandle, LockLostError, SyncLockProvider
from app.services.tenant_context import ConnectionProvider, StaticConnections, TenantConnectionError


class MappingProvider(Protocol):
    def get(self, tenant_id: str) -> FieldMapper: ...

logger = logging.getLogger(__name__)

_FIELD_ADAPTERS = {
    name: TypeAdapter(field.annotation) for name, field in CustomerSyncPayload.model_fields.items()
}


class OwnershipLost(RuntimeError):
    """Raised by a caller-supplied guard: this process may no longer write (e.g. lease deadline passed)."""

    code = "processing_deadline_exceeded"


class OwnershipViolationError(RuntimeError):
    """Sync attempted to write a destination-owned field. Indicates a bug."""


class _SyncFailure(Exception):
    def __init__(self, stage: SyncStage, error: SyncErrorDetail) -> None:
        super().__init__(error.message)
        self.stage = stage
        self.error = error


class HistoryPersistenceError(RuntimeError):
    """The sync ran but its record could not be saved.

    The full record (identifiers only, no account data) is logged at ERROR
    level and attached here, so it isn't lost and callers can report it.
    """

    def __init__(self, record: SyncRecord) -> None:
        super().__init__(f"sync history not persisted for record {record.id}")
        self.record = record


class AccountSyncService:
    def __init__(
        self,
        *,
        crm: CRMClient | None = None,
        destination: InternalSystemClient | None = None,
        mappings: MappingProvider,
        history: SyncHistoryRepository,
        locks: SyncLockProvider | None = None,
        connections: ConnectionProvider | None = None,
    ) -> None:
        if connections is None:
            if crm is None or destination is None:
                raise ValueError("provide either connections or both crm and destination")
            connections = StaticConnections(crm, destination)
        self._connections = connections
        self._mappings = mappings
        self._history = history
        self._locks: SyncLockProvider = locks or InProcessSyncLocks()

    # ------------------------------------------------------------------ #
    def sync_account(self, tenant_id: str, crm_account_id: str,
                     *, guard: Callable[[], None] | None = None) -> SyncRecord:
        """Run one sync and persist its record.

        Raises ``HistoryPersistenceError`` only if the record can't be saved.
        Every other outcome is returned as a ``SyncRecord``.
        """
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        try:
            with self._locks.hold(tenant_id, crm_account_id) as lock:
                record = self._execute(tenant_id, crm_account_id, started_at, t0,
                                       _WriteGate(lock, guard))
                # Persist while still holding the lock so history order matches sync order.
                self._persist(record)
                return record
        except LockError as exc:
            record = self._build(
                tenant_id, crm_account_id, started_at, t0, SyncStatus.FAILED, {}, {},
                SyncStage.ACQUIRE_LOCK,
                SyncErrorDetail(code=exc.code, message=str(exc), retryable=exc.retryable),
            )
            self._persist(record)
            return record

    def _execute(self, tenant_id: str, crm_account_id: str, started_at: datetime,
                 t0: float, gate: _WriteGate | None = None) -> SyncRecord:
        ctx: dict[str, Any] = {}
        try:
            status, extra = self._run(tenant_id, crm_account_id, ctx, gate or _WriteGate(None, None))
            stage, error = None, None
        except _SyncFailure as failure:
            status, extra, stage, error = SyncStatus.FAILED, {}, failure.stage, failure.error
        except Exception:  # noqa: BLE001 - recorded and logged, never silent
            logger.exception("sync.unexpected_error tenant=%s account=%s", tenant_id, crm_account_id)
            status, extra, stage = SyncStatus.FAILED, {}, SyncStage.INTERNAL
            error = SyncErrorDetail(code="unexpected_error",
                                    message="Unexpected internal error; see service logs",
                                    retryable=False)
        return self._build(tenant_id, crm_account_id, started_at, t0, status, extra, ctx, stage, error)

    @staticmethod
    def _build(tenant_id: str, crm_account_id: str, started_at: datetime, t0: float,
               status: SyncStatus, extra: dict, ctx: dict, stage: SyncStage | None,
               error: SyncErrorDetail | None) -> SyncRecord:
        return SyncRecord(
            tenant_id=tenant_id,
            crm_account_id=crm_account_id,
            status=status,
            stage=stage,
            error=error,
            mapping_version=ctx.get("mapping_version"),
            source_last_modified=ctx.get("source_last_modified"),
            destination_customer_id=extra.get("customer_id") or ctx.get("customer_id"),
            fields_changed=extra.get("fields_changed", []),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

    def _persist(self, record: SyncRecord) -> None:
        try:
            self._history.add(record)
        except Exception as exc:
            # Never lose an outcome silently: the record has no account data, so log it in full.
            logger.error("sync.history_write_failed error=%s record=%s",
                         type(exc).__name__, record.model_dump_json())
            raise HistoryPersistenceError(record) from exc
        stage, error = record.stage, record.error
        log = logger.warning if record.status is SyncStatus.FAILED else logger.info
        log("sync.completed tenant=%s account=%s status=%s stage=%s error=%s fields=%s duration_ms=%d",
            record.tenant_id, record.crm_account_id, record.status.value,
            stage.value if stage else "-", error.code if error else "-",
            ",".join(record.fields_changed) or "-", record.duration_ms)

    # ------------------------------------------------------------------ #
    def _run(self, tenant_id: str, crm_account_id: str, ctx: dict[str, Any],
             gate: _WriteGate) -> tuple[SyncStatus, dict]:
        # 1. Resolve the tenant's mapping (fail fast before any network I/O).
        try:
            mapper = self._mappings.get(tenant_id)
        except MappingConfigError as exc:
            raise _SyncFailure(SyncStage.TRANSFORM, SyncErrorDetail(
                code="mapping_config_invalid", message=str(exc), retryable=False)) from exc
        ctx["mapping_version"] = mapper.version

        # 2. Resolve this tenant's own clients (credentials never shared across tenants).
        try:
            clients = self._connections.for_tenant(tenant_id)
        except TenantConnectionError as exc:
            raise _SyncFailure(SyncStage.FETCH_SOURCE, SyncErrorDetail(
                code=exc.code, message=str(exc), retryable=False)) from exc

        # 3. Extract from the CRM.
        try:
            source = clients.crm.get_account(crm_account_id)
        except IntegrationError as exc:
            raise _SyncFailure(SyncStage.FETCH_SOURCE, _detail(exc)) from exc

        # 4. Transform + validate.
        try:
            payload = mapper.map(source)
        except MappingError as exc:
            raise _SyncFailure(SyncStage.TRANSFORM, SyncErrorDetail(
                code=MappingError.code, message="Source record failed mapping validation",
                retryable=False, issues=[i.as_dict() for i in exc.issues])) from exc
        ctx["source_last_modified"] = payload.crm_last_modified_at
        desired = payload.model_dump(mode="python", exclude_unset=True)

        # 5. Load into the destination.
        try:
            return self._load(clients.destination, tenant_id, payload.external_crm_id, desired, ctx, gate)
        except IntegrationError as exc:
            raise _SyncFailure(SyncStage.LOAD_DESTINATION, _detail(exc)) from exc
        except (LockLostError, OwnershipLost) as exc:
            # Refused to write: another worker may now own this account/event. Safe to retry.
            raise _SyncFailure(SyncStage.LOAD_DESTINATION, SyncErrorDetail(
                code=exc.code, message=str(exc), retryable=True)) from exc

    def _load(self, destination: InternalSystemClient, tenant_id: str, external_id: str,
              desired: dict[str, Any], ctx: dict[str, Any], gate: _WriteGate) -> tuple[SyncStatus, dict]:
        _assert_crm_owned_only(desired)
        existing = destination.get_customer_by_external_id(external_id)

        if existing is None:
            body = _to_json(desired)
            try:
                gate.check()  # ownership verified immediately before the destination write
                created = destination.create_customer(
                    body, idempotency_key=_idempotency_key(tenant_id, body))
                return SyncStatus.CREATED, {"customer_id": created["customer_id"],
                                            "fields_changed": sorted(desired)}
            except InternalConflictError:
                # Another writer created it between our read and our create. Fall through to update.
                existing = destination.get_customer_by_external_id(external_id)
                if existing is None:
                    raise

        ctx["customer_id"] = existing["customer_id"]

        if _is_stale(desired.get("crm_last_modified_at"), existing.get("crm_last_modified_at")):
            return SyncStatus.SKIPPED_STALE, {}

        changes = {k: v for k, v in desired.items() if not _equal(k, v, existing.get(k))}
        if not changes:
            return SyncStatus.UNCHANGED, {}

        _assert_crm_owned_only(changes)
        gate.check()  # ownership verified immediately before the destination write
        destination.update_customer(existing["customer_id"], _to_json(changes))
        return SyncStatus.UPDATED, {"fields_changed": sorted(changes)}


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
class _WriteGate:
    """Checks run immediately before every destination write.

    1. The per-account advisory lock is still held by this session (catches a
       lost lock: killed session, idle backstop, failover).
    2. An optional caller guard (the worker's lease-based processing deadline).

    Limitation: this narrows but does not close the window. A lock lost
    *during* the HTTP write itself isn't detected. Closing it fully needs
    destination-side fencing (If-Match/version). See architecture.md.
    """

    def __init__(self, lock: LockHandle | None, guard: Callable[[], None] | None) -> None:
        self._lock = lock
        self._guard = guard

    def check(self) -> None:
        if self._guard is not None:
            self._guard()
        if self._lock is not None:
            self._lock.verify()


def _detail(exc: IntegrationError) -> SyncErrorDetail:
    return SyncErrorDetail(code=exc.code, message=str(exc), retryable=exc.retryable,
                           upstream_status=exc.status_code)


def _assert_crm_owned_only(fields: dict[str, Any]) -> None:
    forbidden = (set(fields) & DESTINATION_OWNED_FIELDS) | (set(fields) - CRM_OWNED_FIELDS)
    if forbidden:
        raise OwnershipViolationError(f"refusing to write non-CRM-owned fields: {sorted(forbidden)}")


def _normalize(field: str, value: Any) -> Any:
    if value is None:
        return None
    try:
        return _FIELD_ADAPTERS[field].validate_python(value)
    except ValidationError:
        return value  # incomparable -> treated as changed


def _equal(field: str, desired: Any, current: Any) -> bool:
    return _normalize(field, desired) == _normalize(field, current)


def _is_stale(incoming: Any, current: Any) -> bool:
    a = _normalize("crm_last_modified_at", incoming)
    b = _normalize("crm_last_modified_at", current)
    return isinstance(a, datetime) and isinstance(b, datetime) and a < b


def _to_json(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(CustomerSyncPayload.model_construct(**data).model_dump_json(include=set(data)))


def _idempotency_key(tenant_id: str, body: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
    return f"{tenant_id}:{body['external_crm_id']}:{digest}"
