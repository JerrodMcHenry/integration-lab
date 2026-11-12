"""Mock Salesforce-style CRM.

Simulates the subset of CRM behaviour IntegrationLab depends on:
- OAuth 2.0 client-credentials token endpoint (``POST /oauth/token``) issuing
  opaque bearer tokens with an enforced expiry.
- Bearer-protected account read and update endpoints.
- Development-only fault injection (``/_admin/*``) for deterministic outage
  and timeout simulation. These routes are not mounted in production-like
  environments (see ``mock_common.guard``).

All state is held in memory per app instance and reset on restart.
"""

from __future__ import annotations

import asyncio
import copy
import os
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from pydantic import BaseModel, Field

from mock_common.guard import admin_routes_enabled

CLIENT_ID = os.getenv("MOCK_CRM_CLIENT_ID", "integrationlab-dev")
CLIENT_SECRET = os.getenv("MOCK_CRM_CLIENT_SECRET", "dev-only-secret")
TOKEN_TTL_SECONDS = int(os.getenv("MOCK_CRM_TOKEN_TTL_SECONDS", "3600"))

SEED_ACCOUNTS = {
    "SF-1001": {
        "Id": "SF-1001",
        "Name": "Acme Capital",
        "Industry": "Financial Services",
        "AnnualRevenue": 2500000,
        "OwnerName": "Sarah Johnson",
        "LastModifiedDate": "2026-11-01T12:00:00+00:00",
    },
    "SF-1002": {
        "Id": "SF-1002",
        "Name": "Meridian Holdings",
        "Industry": "Investment Management",
        "AnnualRevenue": 7500000,
        "OwnerName": "Michael Chen",
        "LastModifiedDate": "2026-11-01T12:00:00+00:00",
    },
}


class FaultConfig(BaseModel):
    mode: Literal["none", "error", "timeout"] = "none"
    remaining: int = Field(default=0, ge=0, le=100)
    status_code: int = Field(default=503, ge=500, le=599)
    delay_seconds: float = Field(default=10.0, ge=0, le=60)


class AccountUpdate(BaseModel):
    Name: str | None = Field(default=None, min_length=1)
    Industry: str | None = None
    AnnualRevenue: float | None = Field(default=None, ge=0)
    OwnerName: str | None = Field(default=None, min_length=1)


class CRMState:
    def __init__(self, *, client_id: str, client_secret: str, token_ttl: int,
                 clock: Callable[[], float]) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_ttl = token_ttl
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self.accounts: dict[str, dict] = copy.deepcopy(SEED_ACCOUNTS)
        self.tokens: dict[str, float] = {}  # access_token -> expiry (clock seconds)
        self.fault = FaultConfig()
        self.account_reads = 0  # observability for retry-bound tests


def create_app(
    *,
    app_env: str | None = None,
    admin_enabled: bool | None = None,
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
    token_ttl: int = TOKEN_TTL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    enable_admin = admin_routes_enabled(app_env) if admin_enabled is None else admin_enabled
    if admin_enabled:
        admin_routes_enabled(app_env, "true")  # still refuse in production

    app = FastAPI(title="IntegrationLab Mock CRM")
    state = CRMState(client_id=client_id, client_secret=client_secret, token_ttl=token_ttl, clock=clock)
    app.state.crm = state
    basic = HTTPBasic(auto_error=False)
    bearer = HTTPBearer(auto_error=False)

    # -- OAuth 2.0 client credentials ------------------------------------ #
    @app.post("/oauth/token")
    async def issue_token(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
        # application/x-www-form-urlencoded per RFC 6749 §4.4.2; parsed with the
        # stdlib to avoid a python-multipart dependency for a single field.
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        grant_type = (form.get("grant_type") or [""])[0]
        if grant_type != "client_credentials":
            raise HTTPException(status_code=400, detail={"error": "unsupported_grant_type"})
        valid = (
            creds is not None
            and secrets.compare_digest(creds.username.encode(), state.client_id.encode())
            and secrets.compare_digest(creds.password.encode(), state.client_secret.encode())
        )
        if not valid:
            raise HTTPException(status_code=401, detail={"error": "invalid_client"})
        now = state.clock()
        # Drop expired tokens so the store stays bounded.
        for tok in [t for t, exp in state.tokens.items() if exp <= now]:
            del state.tokens[tok]
        token = secrets.token_urlsafe(32)
        state.tokens[token] = now + state.token_ttl
        return {"access_token": token, "token_type": "Bearer", "expires_in": state.token_ttl}

    def require_token(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        expiry = state.tokens.get(creds.credentials) if creds else None
        if expiry is None or expiry <= state.clock():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )

    async def apply_fault() -> None:
        state.account_reads += 1
        f = state.fault
        if f.mode == "none" or f.remaining <= 0:
            return
        state.fault = f.model_copy(update={"remaining": f.remaining - 1})
        if f.mode == "timeout":
            await asyncio.sleep(f.delay_seconds)
        raise HTTPException(status_code=f.status_code, detail="Simulated CRM failure")

    # -- Accounts --------------------------------------------------------- #
    @app.get("/crm/accounts/{account_id}", dependencies=[Depends(require_token), Depends(apply_fault)])
    def get_crm_account(account_id: str):
        account = state.accounts.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
        return account

    @app.patch("/crm/accounts/{account_id}", dependencies=[Depends(require_token)])
    def update_crm_account(account_id: str, update: AccountUpdate):
        account = state.accounts.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
        account.update(update.model_dump(exclude_unset=True))
        account["LastModifiedDate"] = datetime.now(timezone.utc).isoformat()
        return account

    # -- Development-only controls --------------------------------------- #
    if enable_admin:
        admin = APIRouter(prefix="/_admin", tags=["dev-admin"])

        @admin.get("/faults")
        def get_faults() -> FaultConfig:
            return state.fault

        @admin.put("/faults")
        def set_faults(config: FaultConfig) -> FaultConfig:
            state.fault = config
            return state.fault

        @admin.put("/accounts/{account_id}", status_code=200)
        def seed_account(account_id: str, account: dict):
            """Overwrite a raw account record (for malformed-data demos)."""
            state.accounts[account_id] = {**account, "Id": account.get("Id", account_id)}
            return state.accounts[account_id]

        @admin.post("/reset", status_code=204)
        def reset() -> None:
            state.reset()

        app.include_router(admin)

    return app


app = create_app()
