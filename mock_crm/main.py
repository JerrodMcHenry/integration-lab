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
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from pydantic import BaseModel, Field

from mock_common.guard import admin_routes_enabled

CLIENT_ID = os.getenv("MOCK_CRM_CLIENT_ID", "integrationlab-dev")
CLIENT_SECRET = os.getenv("MOCK_CRM_CLIENT_SECRET", "dev-only-secret")
TOKEN_TTL_SECONDS = int(os.getenv("MOCK_CRM_TOKEN_TTL_SECONDS", "3600"))
#: Which customer's CRM org this instance simulates (each tenant has its own org).
DATASET = os.getenv("MOCK_CRM_DATASET", "meridian")
#: Outbound webhooks (like Salesforce outbound messages / platform events). Disabled if unset.
WEBHOOK_URL = os.getenv("MOCK_CRM_WEBHOOK_URL") or None
WEBHOOK_SECRET = os.getenv("MOCK_CRM_WEBHOOK_SECRET") or None
logger = logging.getLogger("mock_crm")


def sign_webhook(secret: str, body: bytes, timestamp: int) -> str:
    """Deliberately independent of app/webhooks/signing.py (interoperability is tested)."""
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


class WebhookSender:
    """Signs and POSTs events, retrying a few times like a real provider."""

    def __init__(self, url: str, secret: str, *, client: httpx.Client | None = None,
                 asynchronous: bool = True, attempts: int = 3) -> None:
        self.url, self.secret = url, secret
        self.client = client or httpx.Client(timeout=5.0)
        self.asynchronous = asynchronous
        self.attempts = attempts
        self.sent: dict[str, bytes] = {}       # event_id -> exact body (for redelivery)
        self.deliveries: list[dict] = []       # audit of every attempt

    def emit(self, event_type: str, account_id: str) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        body = json.dumps({"event_id": event_id, "event_type": event_type,
                           "occurred_at": datetime.now(timezone.utc).isoformat(),
                           "data": {"account_id": account_id}}, separators=(",", ":")).encode()
        self.sent[event_id] = body
        self._dispatch(event_id, body)
        return event_id

    def redeliver(self, event_id: str) -> None:
        self._dispatch(event_id, self.sent[event_id])  # same body, fresh signature timestamp

    def _dispatch(self, event_id: str, body: bytes) -> None:
        if self.asynchronous:
            threading.Thread(target=self._deliver, args=(event_id, body), daemon=True).start()
        else:
            self._deliver(event_id, body)

    def _deliver(self, event_id: str, body: bytes) -> None:
        for attempt in range(1, self.attempts + 1):
            header = sign_webhook(self.secret, body, int(time.time()))
            try:
                r = self.client.post(self.url, content=body, headers={
                    "Content-Type": "application/json", "X-IntegrationLab-Signature": header})
                status_code = r.status_code
            except httpx.HTTPError as exc:
                status_code = f"error:{type(exc).__name__}"
            self.deliveries.append({"event_id": event_id, "attempt": attempt, "status": status_code})
            if isinstance(status_code, int) and status_code < 500:
                return  # 2xx accepted/duplicate, or 4xx that a retry won't fix
            time.sleep(min(0.5 * attempt, 2))

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


HARBOR_ACCOUNTS = {
    "HB-2001": {
        "Id": "HB-2001",
        "Name": "Tidewater Pension Fund",
        "Industry": "Pension Fund",
        "AnnualRevenue": 41000000,
        "OwnerName": "Priya Raman",
        "LastModifiedDate": "2026-11-01T12:00:00+00:00",
    },
    "HB-2002": {
        "Id": "HB-2002",
        "Name": "Lighthouse Family Office",
        "Industry": None,
        "AnnualRevenue": 3200000,
        "OwnerName": "Tom Okafor",
        "LastModifiedDate": "2026-11-01T12:00:00+00:00",
    },
}

DATASETS = {"meridian": SEED_ACCOUNTS, "harbor": HARBOR_ACCOUNTS}


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
                 clock: Callable[[], float], dataset: str = "meridian") -> None:
        if dataset not in DATASETS:
            raise ValueError(f"unknown MOCK_CRM_DATASET {dataset!r}")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_ttl = token_ttl
        self.clock = clock
        self.dataset = dataset
        self.reset()

    def reset(self) -> None:
        self.accounts: dict[str, dict] = copy.deepcopy(DATASETS[self.dataset])
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
    dataset: str = DATASET,
    webhook_sender: WebhookSender | None = None,
) -> FastAPI:
    enable_admin = admin_routes_enabled(app_env) if admin_enabled is None else admin_enabled
    if admin_enabled:
        admin_routes_enabled(app_env, "true")  # still refuse in production

    app = FastAPI(title="IntegrationLab Mock CRM")
    state = CRMState(client_id=client_id, client_secret=client_secret, token_ttl=token_ttl, clock=clock,
                     dataset=dataset)
    app.state.crm = state
    sender = webhook_sender or (WebhookSender(WEBHOOK_URL, WEBHOOK_SECRET) if WEBHOOK_URL and WEBHOOK_SECRET else None)
    app.state.webhooks = sender
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
        if sender:
            sender.emit("account.updated", account_id)
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

        @admin.get("/webhooks")
        def webhook_log():
            if not sender:
                raise HTTPException(status_code=404, detail="webhooks not configured")
            return {"url": sender.url, "deliveries": sender.deliveries[-100:]}

        @admin.post("/webhooks/redeliver/{event_id}", status_code=202)
        def redeliver(event_id: str):
            """Resend an already-sent event (same body): demonstrates duplicate handling."""
            if not sender or event_id not in sender.sent:
                raise HTTPException(status_code=404, detail="unknown event")
            sender.redeliver(event_id)
            return {"redelivered": event_id}

        app.include_router(admin)

    return app


app = create_app()
