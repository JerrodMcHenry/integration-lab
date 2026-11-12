"""Webhook ingestion: authenticate, validate, persist. No processing happens here.

Trust model: the tenant id in the URL is only a *routing hint*. The request is
attributed to that tenant only after the body's HMAC verifies with **that
tenant's** secret. Tenant fields inside the payload are never trusted; if one
is present and disagrees with the verified tenant, the event is rejected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import WebhookSettings
from app.repositories.tenants import TenantRepository
from app.repositories.webhook_events import IngestResult, NewEvent, PostgresWebhookEventRepository
from app.services.secrets import EnvSecretResolver, SecretResolutionError
from app.webhooks.signing import SignatureError, TimestampOutsideTolerance, verify

logger = logging.getLogger(__name__)

_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SUPPORTED_EVENT_TYPES = frozenset({"account.created", "account.updated"})


class _Data(BaseModel):
    model_config = ConfigDict(extra="ignore")
    account_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class CRMEventPayload(BaseModel):
    """Envelope we accept. Extra provider fields are ignored and never stored."""

    model_config = ConfigDict(extra="ignore")
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    event_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z]+(\.[a-z_]+)+$")
    occurred_at: datetime | None = None
    tenant_id: str | None = None  # informational only; must match the verified tenant if present
    data: _Data | None = None


class WebhookRejected(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Accepted:
    result: IngestResult
    event_id: str
    outcome: Literal["accepted", "duplicate", "conflict", "ignored"]


class WebhookIngestor:
    def __init__(self, *, tenants: TenantRepository, events: PostgresWebhookEventRepository,
                 secrets: EnvSecretResolver, settings: WebhookSettings) -> None:
        self._tenants = tenants
        self._events = events
        self._secrets = secrets
        self._settings = settings

    def ingest(self, tenant_id: str, signature_header: str | None, body: bytes) -> Accepted:
        generic = WebhookRejected(401, "signature verification failed")

        # 1. Resolve the claimed tenant's secret(s). Every failure looks identical to a bad signature.
        if not _TENANT_ID.match(tenant_id):
            raise generic
        tenant = self._tenants.get_tenant(tenant_id)
        integration = self._tenants.get_integration(tenant_id) if tenant else None
        refs = [r for r in (integration.webhook_secret_ref if integration else None,
                            integration.webhook_secret_previous_ref if integration else None) if r]
        secrets: list[str] = []
        for ref in refs:
            try:
                secrets.append(self._secrets.resolve(tenant_id, ref))
            except SecretResolutionError as exc:
                logger.error("webhook.secret_unavailable tenant=%s reason=%s", tenant_id, exc)
        if tenant is None or not secrets:
            logger.warning("webhook.rejected reason=no_tenant_or_secret tenant_hint=%s", tenant_id)
            raise generic

        # 2. Authenticate the raw bytes, then enforce the replay window.
        try:
            signed_ts = verify(signature_header, body, secrets,
                               tolerance_seconds=self._settings.tolerance_seconds)
        except TimestampOutsideTolerance as exc:
            logger.warning("webhook.rejected reason=timestamp_outside_tolerance tenant=%s", tenant_id)
            raise WebhookRejected(401, "signature timestamp outside tolerance") from exc
        except SignatureError as exc:
            logger.warning("webhook.rejected reason=bad_signature tenant_hint=%s", tenant_id)
            raise generic from exc

        # From here on the sender is authenticated as `tenant_id`.
        if not tenant.active:
            raise WebhookRejected(403, "tenant is suspended")

        # 3. Validate the envelope.
        try:
            payload = CRMEventPayload.model_validate(json.loads(body))
        except (ValueError, ValidationError) as exc:
            raise WebhookRejected(422, "invalid event payload") from exc
        if payload.tenant_id is not None and payload.tenant_id != tenant_id:
            logger.warning("webhook.rejected reason=payload_tenant_mismatch tenant=%s", tenant_id)
            raise WebhookRejected(422, "payload tenant does not match the authenticated tenant")
        supported = payload.event_type in SUPPORTED_EVENT_TYPES
        if supported and payload.data is None:
            raise WebhookRejected(422, "account events require data.account_id")

        # 4. Persist (committed before we return). Only an allowlisted envelope is stored.
        envelope = {"event_id": payload.event_id, "event_type": payload.event_type,
                    "occurred_at": payload.occurred_at.isoformat() if payload.occurred_at else None,
                    "account_id": payload.data.account_id if payload.data else None}
        result = self._events.ingest(NewEvent(
            tenant_id=tenant_id, source="crm", event_id=payload.event_id, event_type=payload.event_type,
            crm_account_id=envelope["account_id"], occurred_at=payload.occurred_at,
            signed_at=datetime.fromtimestamp(signed_ts, tz=timezone.utc), payload=envelope,
            payload_sha256=hashlib.sha256(body).hexdigest(), ignored=not supported))

        if not result.created:
            outcome = "duplicate" if result.same_content else "conflict"
        else:
            outcome = "accepted" if supported else "ignored"
        logger.info("webhook.%s tenant=%s event_id=%s type=%s id=%s", outcome, tenant_id,
                    payload.event_id, payload.event_type, result.id)
        return Accepted(result, payload.event_id, outcome)
