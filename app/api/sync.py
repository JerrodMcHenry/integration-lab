"""Account synchronization endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from app.api.deps import get_current_tenant, get_sync_history, get_sync_service
from app.repositories.sync_history import SyncHistoryRepository
from app.schemas.sync import SyncRecord, SyncStage, SyncStatus
from app.services.account_sync import AccountSyncService, HistoryPersistenceError

router = APIRouter(prefix="/sync", tags=["sync"])

CrmAccountId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def _http_status(record: SyncRecord) -> int:
    if record.status is SyncStatus.CREATED:
        return 201
    if record.status is not SyncStatus.FAILED:
        return 200
    err = record.error
    if record.stage is SyncStage.ACQUIRE_LOCK:
        return 409 if err and err.code == "sync_lock_timeout" else 503
    if record.stage is SyncStage.FETCH_SOURCE and err and err.code == "crm_not_found":
        return 404
    if record.stage is SyncStage.TRANSFORM:
        return 422
    if record.stage is SyncStage.INTERNAL:
        return 500
    if err and err.code.endswith("_timeout"):
        return 504
    return 502


@router.post(
    "/accounts/{crm_account_id}",
    response_model=SyncRecord,
    responses={
        201: {"model": SyncRecord, "description": "Customer created in destination"},
        404: {"model": SyncRecord, "description": "Account not found in CRM"},
        409: {"model": SyncRecord, "description": "Another sync of this account is still running (lock timeout)"},
        422: {"model": SyncRecord, "description": "Mapping or validation failure"},
        502: {"model": SyncRecord, "description": "Upstream system error"},
        503: {"model": SyncRecord, "description": "Database unavailable, or the sync ran but its "
              "history record could not be saved (header X-Sync-History-Persisted: false)"},
        504: {"model": SyncRecord, "description": "Upstream system timeout"},
    },
    summary="Synchronize one CRM account into the internal customer system",
)
def sync_account(
    crm_account_id: CrmAccountId,
    tenant_id: Annotated[str, Depends(get_current_tenant)],
    service: Annotated[AccountSyncService, Depends(get_sync_service)],
):
    try:
        record = service.sync_account(tenant_id, crm_account_id)
    except HistoryPersistenceError as exc:
        # The destination may already have been updated; tell the caller exactly that.
        return JSONResponse(status_code=503, content=exc.record.model_dump(mode="json"),
                            headers={"X-Sync-History-Persisted": "false"})
    return JSONResponse(status_code=_http_status(record), content=record.model_dump(mode="json"),
                        headers={"X-Sync-History-Persisted": "true"})


@router.get("/history", response_model=list[SyncRecord], summary="Recent sync attempts for the tenant")
def list_history(
    tenant_id: Annotated[str, Depends(get_current_tenant)],
    history: Annotated[SyncHistoryRepository, Depends(get_sync_history)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    crm_account_id: str | None = None,
    status: SyncStatus | None = None,
):
    return history.list_recent(tenant_id, limit=limit, crm_account_id=crm_account_id, status=status)


@router.get("/history/{record_id}", response_model=SyncRecord)
def get_history_record(
    record_id: UUID,
    tenant_id: Annotated[str, Depends(get_current_tenant)],
    history: Annotated[SyncHistoryRepository, Depends(get_sync_history)],
):
    record = history.get(tenant_id, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Sync record not found")
    return record
