"""Operational metrics, worker health and the audit log."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.api.dependencies import get_audit_repository, get_operations_repository, require
from app.auth.permissions import Permission
from app.auth.tokens import Principal
from app.core.config import get_settings
from app.repositories.audit import AuditRepository
from app.db.errors import RepositoryUnavailableError
from app.repositories.operations import PostgresOperationsRepository
from app.worker.runner import resolve_lease_seconds

router = APIRouter(tags=["operations"])
_ZERO = {"pending": 0, "ready": 0, "retry_backlog": 0, "processing": 0, "expired_leases": 0, "failed": 0,
         "dead_letter": 0, "succeeded_1h": 0, "oldest_pending_s": None, "latency_p50_s": None,
         "latency_p95_s": None, "latency_max_s": None}


def _stale_after() -> float:
    return resolve_lease_seconds(get_settings())


class EventMetrics(BaseModel):
    pending: int
    ready: int
    retry_backlog: int
    processing: int
    expired_leases: int
    failed: int
    dead_letter: int
    succeeded_1h: int
    oldest_pending_s: float | None
    latency_p50_s: float | None
    latency_p95_s: float | None
    latency_max_s: float | None


class WorkerSummary(BaseModel):
    healthy: int
    total_seen: int
    last_heartbeat_at: datetime | None


class OpsMetricsResponse(BaseModel):
    tenant_id: str
    events: EventMetrics
    workers: WorkerSummary


@router.get("/ops/metrics", response_model=OpsMetricsResponse,
            summary="Event queue metrics for the caller's tenant + worker health (requires history:read)")
def ops_metrics(principal: Annotated[Principal, Depends(require(Permission.HISTORY_READ))],
                repo: Annotated[PostgresOperationsRepository, Depends(get_operations_repository)]):
    try:
        rows = repo.metrics(principal.tenant_id)
        workers = repo.workers(stale_after_seconds=_stale_after())
    except RepositoryUnavailableError as exc:
        raise HTTPException(status_code=503, detail="metrics unavailable") from exc
    m = {**_ZERO, **{k: v for k, v in (rows[0] if rows else {}).items() if k != "tenant_id"}}
    m = {k: (float(v) if isinstance(v, (int, float)) and k.endswith("_s") and v is not None else v) for k, v in m.items()}
    # Worker identities are platform details; tenants only see aggregate health.
    return OpsMetricsResponse(tenant_id=principal.tenant_id, events=EventMetrics(**m), workers=WorkerSummary(
        healthy=sum(1 for w in workers if w["healthy"]), total_seen=len(workers),
        last_heartbeat_at=max((w["last_seen_at"] for w in workers), default=None)))


class AuditResponse(BaseModel):
    occurred_at: datetime | None
    actor: str
    actor_client_id: str | None
    action: str
    outcome: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]


@router.get("/admin/audit", response_model=list[AuditResponse],
            summary="Audit log for the caller's tenant (requires config:read)")
def audit_log(principal: Annotated[Principal, Depends(require(Permission.CONFIG_READ))],
              audit: Annotated[AuditRepository, Depends(get_audit_repository)],
              limit: Annotated[int, Query(ge=1, le=500)] = 100,
              action: Annotated[str | None, Query(pattern=r"^[a-z_]+(\.[a-z_]+)+$")] = None):
    try:
        entries = audit.list_recent(principal.tenant_id, limit=limit, action=action)
    except RepositoryUnavailableError as exc:
        raise HTTPException(status_code=503, detail="audit log unavailable") from exc
    return [AuditResponse(occurred_at=e.occurred_at, actor=e.actor, actor_client_id=e.actor_client_id,
                          action=e.action, outcome=e.outcome, target_type=e.target_type, target_id=e.target_id,
                          details=e.details) for e in entries]


# --------------------------------------------------------------------------- #
# Platform-wide Prometheus metrics (all tenants): separate static bearer token
# --------------------------------------------------------------------------- #
@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def prometheus(repo: Annotated[PostgresOperationsRepository, Depends(get_operations_repository)],
               authorization: Annotated[str | None, Header()] = None):
    token = get_settings().metrics_token
    if not token:
        raise HTTPException(status_code=404, detail="Not Found")  # disabled unless configured
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="invalid metrics token", headers={"WWW-Authenticate": "Bearer"})
    try:
        rows = repo.metrics(None)
        workers = repo.workers(stale_after_seconds=_stale_after())
    except RepositoryUnavailableError as exc:
        raise HTTPException(status_code=503, detail="metrics unavailable") from exc
    lines = []
    gauges = {
        "pending": "Events waiting to be processed (including scheduled retries)",
        "ready": "Pending events claimable now",
        "retry_backlog": "Pending events that already failed at least once",
        "processing": "Events currently leased by a worker",
        "expired_leases": "Processing events whose lease expired (crashed workers)",
        "failed": "Permanently failed events",
        "dead_letter": "Events that exhausted retries",
        "succeeded_1h": "Events succeeded in the last hour",
        "oldest_pending_s": "Age of the oldest pending event in seconds",
        "latency_p95_s": "p95 receive-to-success latency over the last hour, seconds",
    }
    for name, help_text in gauges.items():
        lines += [f"# HELP integrationlab_events_{name} {help_text}", f"# TYPE integrationlab_events_{name} gauge"]
        for r in rows:
            if r[name] is not None:
                lines.append(f'integrationlab_events_{name}{{tenant="{r["tenant_id"]}"}} {float(r[name])}')
    lines += ["# HELP integrationlab_workers_healthy Workers with a recent heartbeat",
              "# TYPE integrationlab_workers_healthy gauge",
              f"integrationlab_workers_healthy {sum(1 for w in workers if w['healthy'])}"]
    return "\n".join(lines) + "\n"
