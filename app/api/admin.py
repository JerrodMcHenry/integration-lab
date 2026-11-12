"""Identity and tenant administration endpoints.

Everything here operates on the caller's own tenant (from the token). There
are no endpoints that take a tenant id as input.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_audit_repository, get_tenant_repository
from app.auth.dependencies import get_principal, require, require_audited
from app.auth.permissions import Permission
from app.auth.tokens import Principal
from app.mapping.mapper import FieldMapper, MappingConfigError
from app.repositories.audit import AuditEntry, AuditRepository
from app.repositories.tenants import MappingVersionConflict, TenantRepository

router = APIRouter(tags=["identity & administration"])


class MeResponse(BaseModel):
    subject: str
    tenant_id: str
    roles: list[str]
    permissions: list[str]


class TenantResponse(BaseModel):
    id: str
    display_name: str
    status: str


class IntegrationResponse(BaseModel):
    """Connection settings with secrets redacted: only whether each is configured."""

    crm_base_url: str
    crm_client_id: str
    crm_client_secret: str
    cms_base_url: str
    cms_api_key: str


class MappingVersionResponse(BaseModel):
    version: int
    created_by: str
    created_at: datetime
    config: dict[str, Any]


class MappingVersionSummary(BaseModel):
    version: int
    created_by: str
    created_at: datetime


@router.get("/me", response_model=MeResponse, summary="Who am I (any authenticated user)")
def me(principal: Annotated[Principal, Depends(get_principal)]):
    return MeResponse(subject=principal.subject, tenant_id=principal.tenant_id,
                      roles=sorted(r.value for r in principal.roles),
                      permissions=sorted(p.value for p in principal.permissions))


@router.get("/tenant", response_model=TenantResponse, summary="Caller's tenant (requires tenant:read)")
def tenant(principal: Annotated[Principal, Depends(require(Permission.TENANT_READ))],
           repo: Annotated[TenantRepository, Depends(get_tenant_repository)]):
    t = repo.get_tenant(principal.tenant_id)
    return TenantResponse(id=t.id, display_name=t.display_name, status=t.status)


@router.get("/admin/integration", response_model=IntegrationResponse,
            summary="Caller's tenant connection settings, secrets redacted (requires config:read)")
def integration(principal: Annotated[Principal, Depends(require(Permission.CONFIG_READ))],
                repo: Annotated[TenantRepository, Depends(get_tenant_repository)]):
    i = repo.get_integration(principal.tenant_id)
    if i is None:
        raise HTTPException(status_code=404, detail="no integration configured")
    # Secret references are internal plumbing too; expose only that they are set.
    return IntegrationResponse(crm_base_url=i.crm_base_url, crm_client_id=i.crm_client_id,
                               crm_client_secret="configured", cms_base_url=i.cms_base_url,
                               cms_api_key="configured")


@router.get("/admin/mapping", response_model=MappingVersionResponse,
            summary="Active mapping configuration (requires config:read)")
def active_mapping(principal: Annotated[Principal, Depends(require(Permission.CONFIG_READ))],
                   repo: Annotated[TenantRepository, Depends(get_tenant_repository)]):
    m = repo.get_active_mapping(principal.tenant_id)
    if m is None:
        raise HTTPException(status_code=404, detail="no mapping configured")
    return MappingVersionResponse(version=m.version, created_by=m.created_by, created_at=m.created_at,
                                  config=m.config)


@router.get("/admin/mapping/versions", response_model=list[MappingVersionSummary],
            summary="Mapping version history (requires config:read)")
def mapping_versions(principal: Annotated[Principal, Depends(require(Permission.CONFIG_READ))],
                     repo: Annotated[TenantRepository, Depends(get_tenant_repository)]):
    return [MappingVersionSummary(version=m.version, created_by=m.created_by, created_at=m.created_at)
            for m in repo.list_mapping_versions(principal.tenant_id)]


@router.put("/admin/mapping", response_model=MappingVersionResponse, status_code=201,
            summary="Publish a new mapping version (requires config:write)",
            responses={409: {"description": "Concurrent publish; retry"},
                       422: {"description": "Invalid mapping, or tenant_id doesn't match the caller"}})
def publish_mapping(principal: Annotated[Principal, Depends(require_audited(Permission.CONFIG_WRITE,
                                                                          "mapping.publish"))],
                    repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
                    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
                    config: Annotated[dict[str, Any], Body(max_length=100)]):
    def record(outcome: str, **details):
        audit.record(AuditEntry(actor=principal.subject, actor_client_id=principal.client_id,
                                tenant_id=principal.tenant_id, action="mapping.publish", outcome=outcome,
                                target_type="mapping", details=details))

    if config.get("tenant_id") != principal.tenant_id:
        # Reject rather than silently rewrite: a mismatch indicates a mistake or an attack.
        record("rejected", reason="tenant_id mismatch")
        raise HTTPException(status_code=422, detail="mapping tenant_id must match your tenant")
    candidate = {**config, "version": 1}  # the server assigns the real version
    try:
        FieldMapper.from_dict(candidate)
    except MappingConfigError as exc:
        record("rejected", reason="invalid mapping", problems=len(exc.problems))
        raise HTTPException(status_code=422, detail={"problems": exc.problems}) from exc
    try:
        stored = repo.add_mapping_version(principal.tenant_id, candidate, principal.subject)
    except MappingVersionConflict as exc:
        record("rejected", reason="concurrent publish")
        raise HTTPException(status_code=409, detail="another mapping version was published concurrently") from exc
    record("success", version=stored.version, fields=len(candidate.get("fields", [])))
    return MappingVersionResponse(version=stored.version, created_by=stored.created_by,
                                  created_at=stored.created_at, config=stored.config)
