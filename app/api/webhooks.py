"""Webhook ingestion endpoint and event inspection endpoints."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_engine, get_tenant_repository
from app.auth.dependencies import require
from app.auth.permissions import Permission
from app.auth.tokens import Principal
from app.core.config import get_settings
from app.repositories.sync_history import RepositoryError, RepositoryUnavailableError
from app.repositories.webhook_events import PostgresWebhookEventRepository
from app.services.secrets import EnvSecretResolver
from app.webhooks.ingest import WebhookIngestor, WebhookRejected
from app.webhooks.signing import SIGNATURE_HEADER

router = APIRouter(tags=["webhooks"])


@lru_cache
def get_webhook_event_repository() -> PostgresWebhookEventRepository | None:
    engine = get_engine()
    return PostgresWebhookEventRepository(engine) if engine else None


def get_webhook_ingestor() -> WebhookIngestor:
    events = get_webhook_event_repository()
    if events is None:
        # Durable ingestion needs the database; never acknowledge without persistence.
        raise HTTPException(status_code=503, detail="webhook ingestion requires a database")
    settings = get_settings()
    return WebhookIngestor(tenants=get_tenant_repository(), events=events,
                           secrets=EnvSecretResolver(app_env=settings.environment), settings=settings.webhooks)


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="payload too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="payload too large")
    return bytes(body)


@router.post(
    "/webhooks/crm/{tenant_id}",
    summary="Receive a signed CRM account-change event",
    status_code=202,
    responses={
        200: {"description": "Duplicate delivery of an event already received (no new work)"},
        202: {"description": "New event persisted; it will be processed asynchronously"},
        401: {"description": "Missing/invalid signature, unknown tenant, or timestamp outside the replay window"},
        403: {"description": "Tenant suspended"},
        409: {"description": "event_id reused with different content"},
        413: {"description": "Body exceeds WEBHOOK_MAX_BODY_BYTES"},
        422: {"description": "Authenticated but the payload is invalid"},
        503: {"description": "Event store unavailable: NOT persisted, sender should retry"},
    },
)
async def receive_crm_webhook(
    tenant_id: str,
    request: Request,
    ingestor: Annotated[WebhookIngestor, Depends(get_webhook_ingestor)],
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
):
    body = await _read_body(request, get_settings().webhooks.max_body_bytes)
    try:
        # Ingestion does blocking DB I/O: keep it off the event loop.
        accepted = await run_in_threadpool(ingestor.ingest, tenant_id, signature, body)
    except WebhookRejected as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    except RepositoryUnavailableError:
        return JSONResponse(status_code=503, content={"detail": "event store unavailable; retry later"})
    except RepositoryError:
        return JSONResponse(status_code=500, content={"detail": "event store error"})

    r = accepted.result
    body_out = {"status": accepted.outcome, "id": str(r.id), "event_id": accepted.event_id,
                "event_status": r.status}
    code = {"accepted": 202, "ignored": 202, "duplicate": 200, "conflict": 409}[accepted.outcome]
    if accepted.outcome == "conflict":
        body_out["detail"] = "event_id was already received with different content"
    return JSONResponse(status_code=code, content=body_out)


# --------------------------------------------------------------------------- #
# Inspection (tenant from token)
# --------------------------------------------------------------------------- #
class EventError(BaseModel):
    code: str
    message: str | None
    retryable: bool | None


class WebhookEventView(BaseModel):
    id: UUID
    event_id: str
    event_type: str
    crm_account_id: str | None
    status: str
    attempts: int
    occurred_at: datetime | None
    received_at: datetime
    processed_at: datetime | None
    last_error: EventError | None
    sync_record_id: UUID | None


def _events_repo() -> PostgresWebhookEventRepository:
    repo = get_webhook_event_repository()
    if repo is None:
        raise HTTPException(status_code=503, detail="event store requires a database")
    return repo


def _to_view(e) -> WebhookEventView:
    err = EventError(code=e.last_error_code, message=e.last_error_message,
                     retryable=e.last_error_retryable) if e.last_error_code else None
    return WebhookEventView(id=e.id, event_id=e.event_id, event_type=e.event_type,
                            crm_account_id=e.crm_account_id, status=e.status, attempts=e.attempts,
                            occurred_at=e.occurred_at, received_at=e.received_at,
                            processed_at=e.processed_at, last_error=err, sync_record_id=e.sync_record_id)


@router.get("/webhooks/events", response_model=list[WebhookEventView],
            summary="Recent inbound events for the caller's tenant (requires history:read)")
def list_events(principal: Annotated[Principal, Depends(require(Permission.HISTORY_READ))],
                repo: Annotated[PostgresWebhookEventRepository, Depends(_events_repo)],
                limit: Annotated[int, Query(ge=1, le=500)] = 50,
                status: Annotated[str | None, Query(pattern=r"^(pending|processing|succeeded|failed|ignored)$")] = None):
    return [_to_view(e) for e in repo.list_recent(principal.tenant_id, limit=limit, status=status)]


@router.get("/webhooks/events/{event_uuid}", response_model=WebhookEventView)
def get_event(event_uuid: UUID, principal: Annotated[Principal, Depends(require(Permission.HISTORY_READ))],
              repo: Annotated[PostgresWebhookEventRepository, Depends(_events_repo)]):
    e = repo.get(principal.tenant_id, event_uuid)
    if e is None:
        raise HTTPException(status_code=404, detail="event not found")
    return _to_view(e)
