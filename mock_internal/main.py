"""Mock of Meridian Capital's internal Customer Management System (CMS).

Realistic behaviours IntegrationLab must cope with:
- Static service API key (bearer) authentication.
- Server-generated ``customer_id``. Lookup by ``external_crm_id`` (unique).
- ``POST`` create with ``Idempotency-Key`` support and ``409`` on duplicate
  external id.
- ``PATCH`` partial update that accepts *any* field, including
  internal-owned ones, because Meridian's own staff tooling uses the same API.
  The destination does **not** protect onboarding/risk fields from
  integrations, so IntegrationLab has to.
- New customers default to ``onboarding_status=pending`` and
  ``risk_classification=unassessed``.
- Development-only fault injection under ``/_admin``.
"""

from __future__ import annotations

import asyncio
import copy
import os
import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from mock_common.guard import admin_routes_enabled

API_KEY = os.getenv("MOCK_INTERNAL_API_KEY", "dev-only-internal-key")

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
OnboardingStatus = Literal["pending", "kyc_in_review", "approved", "rejected", "offboarded"]
RiskClass = Literal["unassessed", "low", "medium", "high"]


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_crm_id: Name
    legal_name: Name
    relationship_manager: Name
    industry_segment: Name | None = None
    annual_revenue_usd: Decimal | None = Field(default=None, ge=0)
    crm_last_modified_at: datetime | None = None
    onboarding_status: OnboardingStatus = "pending"
    risk_classification: RiskClass = "unassessed"


class CustomerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    legal_name: Name | None = None
    relationship_manager: Name | None = None
    industry_segment: Name | None = None
    annual_revenue_usd: Decimal | None = Field(default=None, ge=0)
    crm_last_modified_at: datetime | None = None
    onboarding_status: OnboardingStatus | None = None
    risk_classification: RiskClass | None = None


_NON_NULLABLE = {"legal_name", "relationship_manager", "onboarding_status", "risk_classification"}


class FaultConfig(BaseModel):
    mode: Literal["none", "error", "timeout"] = "none"
    remaining: int = Field(default=0, ge=0, le=100)
    status_code: int = Field(default=503, ge=500, le=599)
    delay_seconds: float = Field(default=10.0, ge=0, le=60)
    #: Restrict faults to writes (POST/PATCH) so reads still succeed.
    writes_only: bool = False


class CMSState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.customers: dict[str, dict] = {}
        self.by_external: dict[str, str] = {}
        self.idempotency: dict[str, tuple[int, dict]] = {}
        self.fault = FaultConfig()
        self.write_count = 0


def create_app(*, app_env: str | None = None, admin_enabled: bool | None = None,
               api_key: str = API_KEY) -> FastAPI:
    enable_admin = admin_routes_enabled(app_env) if admin_enabled is None else admin_enabled
    if admin_enabled:
        admin_routes_enabled(app_env, "true")  # still refuse in production

    app = FastAPI(title="Meridian Customer Management System (mock)")
    state = CMSState()
    app.state.cms = state
    bearer = HTTPBearer(auto_error=False)

    def require_key(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if creds is None or not secrets.compare_digest(creds.credentials.encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="invalid_api_key",
                                headers={"WWW-Authenticate": "Bearer"})

    def fault_dep(is_write: bool):
        async def _apply() -> None:
            f = state.fault
            if f.mode == "none" or f.remaining <= 0 or (f.writes_only and not is_write):
                return
            state.fault = f.model_copy(update={"remaining": f.remaining - 1})
            if f.mode == "timeout":
                await asyncio.sleep(f.delay_seconds)
            raise HTTPException(status_code=f.status_code, detail="Simulated CMS failure")
        return Depends(_apply)

    read_deps = [Depends(require_key), fault_dep(False)]
    write_deps = [Depends(require_key), fault_dep(True)]

    def _get_or_404(customer_id: str) -> dict:
        c = state.customers.get(customer_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        return c

    @app.get("/api/v1/customers", dependencies=read_deps)
    def list_customers():
        return {"items": list(state.customers.values())}

    @app.get("/api/v1/customers/by-external-id/{external_crm_id}", dependencies=read_deps)
    def get_by_external(external_crm_id: str):
        cid = state.by_external.get(external_crm_id)
        if cid is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        return state.customers[cid]

    @app.get("/api/v1/customers/{customer_id}", dependencies=read_deps)
    def get_customer(customer_id: str):
        return _get_or_404(customer_id)

    @app.post("/api/v1/customers", status_code=201, dependencies=write_deps)
    def create_customer(body: CustomerCreate,
                        idempotency_key: Annotated[str | None, Header(max_length=200)] = None):
        if idempotency_key and idempotency_key in state.idempotency:
            code, payload = state.idempotency[idempotency_key]
            return JSONResponse(status_code=code, content=payload)
        existing = state.by_external.get(body.external_crm_id)
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail={"error": "duplicate_external_crm_id", "customer_id": existing})
        now = datetime.now(timezone.utc)
        record = {"customer_id": f"CUS-{uuid.uuid4().hex[:12]}", **body.model_dump(),
                  "created_at": now, "updated_at": now}
        record = jsonable_encoder(record)
        state.customers[record["customer_id"]] = record
        state.by_external[body.external_crm_id] = record["customer_id"]
        state.write_count += 1
        if idempotency_key:
            state.idempotency[idempotency_key] = (201, copy.deepcopy(record))
        return JSONResponse(status_code=201, content=record)

    @app.patch("/api/v1/customers/{customer_id}", dependencies=write_deps)
    def patch_customer(customer_id: str, body: CustomerPatch):
        record = _get_or_404(customer_id)
        changes = body.model_dump(exclude_unset=True)
        nulls = [k for k, v in changes.items() if v is None and k in _NON_NULLABLE]
        if nulls:
            raise HTTPException(status_code=422, detail={"error": "non_nullable", "fields": nulls})
        record.update(jsonable_encoder(changes))
        record["updated_at"] = jsonable_encoder(datetime.now(timezone.utc))
        state.write_count += 1
        return record

    if enable_admin:
        admin = APIRouter(prefix="/_admin", tags=["dev-admin"])

        @admin.get("/faults")
        def get_faults() -> FaultConfig:
            return state.fault

        @admin.put("/faults")
        def set_faults(config: FaultConfig) -> FaultConfig:
            state.fault = config
            return state.fault

        @admin.post("/reset", status_code=204)
        def reset() -> None:
            state.reset()

        app.include_router(admin)

    return app


app = create_app()
