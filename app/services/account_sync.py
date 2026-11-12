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
import threading
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.integrations.crm_client import CRMClient
from app.integrations.errors import IntegrationError
from app.integrations.internal_client import InternalConflictError, InternalSystemClient
from app.mapping.mapper import MappingConfigError, MappingError, MappingRegistry
from app.repositories.sync_history import SyncHistoryRepository
from app.schemas.internal_customer import (
    CRM_OWNED_FIELDS,
    DESTINATION_OWNED_FIELDS,
    CustomerSyncPayload,
)
from app.schemas.sync import SyncErrorDetail, SyncRecord, SyncStage, SyncStatus

logger = logging.getLogger(__name__)

_FIELD_ADAPTERS = {
    name: TypeAdapter(field.annotation) for name, field in CustomerSyncPayload.model_fields.items()
}


class OwnershipViolationError(RuntimeError):
    """Sync attempted to write a destination-owned field. Indicates a bug."""


class _SyncFailure(Exception):
    def __init__(self, stage: SyncStage, error: SyncErrorDetail) -> None:
        super().__init__(error.message)
        self.stage = stage
        self.error = error


class _StripedLocks:
    """Serializes concurrent syncs of the same account within this process.

    Uses a fixed number of locks so memory stays bounded. Cross-process
    serialization needs a database lock (Milestone 4).
    """

    def __init__(self, stripes: int = 64) -> None:
        self._locks = [threading.Lock() for _ in range(stripes)]

    def for_key(self, key: str) -> threading.Lock:
        return self._locks[hash(key) % len(self._locks)]


class AccountSyncService:
    def __init__(
        self,
        *,
        crm: CRMClient,
        destination: InternalSystemClient,
        mappings: MappingRegistry,
        history: SyncHistoryRepository,
    ) -> None:
        self._crm = crm
        self._destination = destination
        self._mappings = mappings
        self._history = history
        self._locks = _StripedLocks()

    # ------------------------------------------------------------------ #
    def sync_account(self, tenant_id: str, crm_account_id: str) -> SyncRecord:
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        ctx: dict[str, Any] = {}

        with self._locks.for_key(f"{tenant_id}:{crm_account_id}"):
            try:
                status, extra = self._run(tenant_id, crm_account_id, ctx)
                stage, error = None, None
            except _SyncFailure as failure:
                status, extra, stage, error = SyncStatus.FAILED, {}, failure.stage, failure.error
            except Exception:  # noqa: BLE001 - recorded and logged, never silent
                logger.exception("sync.unexpected_error tenant=%s account=%s", tenant_id, crm_account_id)
                status, extra, stage = SyncStatus.FAILED, {}, SyncStage.INTERNAL
                error = SyncErrorDetail(code="unexpected_error",
                                        message="Unexpected internal error; see service logs",
                                        retryable=False)

        record = SyncRecord(
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
        self._history.add(record)
        log = logger.warning if status is SyncStatus.FAILED else logger.info
        log("sync.completed tenant=%s account=%s status=%s stage=%s error=%s fields=%s duration_ms=%d",
            tenant_id, crm_account_id, status.value, stage.value if stage else "-",
            error.code if error else "-", ",".join(record.fields_changed) or "-", record.duration_ms)
        return record

    # ------------------------------------------------------------------ #
    def _run(self, tenant_id: str, crm_account_id: str, ctx: dict[str, Any]) -> tuple[SyncStatus, dict]:
        # 1. Resolve the tenant's mapping (fail fast before any network I/O).
        try:
            mapper = self._mappings.get(tenant_id)
        except MappingConfigError as exc:
            raise _SyncFailure(SyncStage.TRANSFORM, SyncErrorDetail(
                code="mapping_config_invalid", message=str(exc), retryable=False)) from exc
        ctx["mapping_version"] = mapper.version

        # 2. Extract from the CRM.
        try:
            source = self._crm.get_account(crm_account_id)
        except IntegrationError as exc:
            raise _SyncFailure(SyncStage.FETCH_SOURCE, _detail(exc)) from exc

        # 3. Transform + validate.
        try:
            payload = mapper.map(source)
        except MappingError as exc:
            raise _SyncFailure(SyncStage.TRANSFORM, SyncErrorDetail(
                code=MappingError.code, message="Source record failed mapping validation",
                retryable=False, issues=[i.as_dict() for i in exc.issues])) from exc
        ctx["source_last_modified"] = payload.crm_last_modified_at
        desired = payload.model_dump(mode="python", exclude_unset=True)

        # 4. Load into the destination.
        try:
            return self._load(tenant_id, payload.external_crm_id, desired, ctx)
        except IntegrationError as exc:
            raise _SyncFailure(SyncStage.LOAD_DESTINATION, _detail(exc)) from exc

    def _load(self, tenant_id: str, external_id: str, desired: dict[str, Any],
              ctx: dict[str, Any]) -> tuple[SyncStatus, dict]:
        _assert_crm_owned_only(desired)
        existing = self._destination.get_customer_by_external_id(external_id)

        if existing is None:
            body = _to_json(desired)
            try:
                created = self._destination.create_customer(
                    body, idempotency_key=_idempotency_key(tenant_id, body))
                return SyncStatus.CREATED, {"customer_id": created["customer_id"],
                                            "fields_changed": sorted(desired)}
            except InternalConflictError:
                # Another writer created it between our read and our create. Fall through to update.
                existing = self._destination.get_customer_by_external_id(external_id)
                if existing is None:
                    raise

        ctx["customer_id"] = existing["customer_id"]

        if _is_stale(desired.get("crm_last_modified_at"), existing.get("crm_last_modified_at")):
            return SyncStatus.SKIPPED_STALE, {}

        changes = {k: v for k, v in desired.items() if not _equal(k, v, existing.get(k))}
        if not changes:
            return SyncStatus.UNCHANGED, {}

        _assert_crm_owned_only(changes)
        self._destination.update_customer(existing["customer_id"], _to_json(changes))
        return SyncStatus.UPDATED, {"fields_changed": sorted(changes)}


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
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
