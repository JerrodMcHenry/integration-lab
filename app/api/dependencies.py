"""FastAPI dependencies: authentication, authorization and access to services.

Each dependency is defined once, here. Objects are constructed in
``app/wiring.py``; these functions only hand them to routes (and give tests
a single override point per dependency).

Tenant identity comes **only** from the verified token's tenant claim, and it
must name an existing, active tenant row. Path, query, header and body values
are never consulted for tenant identity.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine

from app import wiring
from app.auth.permissions import Permission
from app.auth.tokens import AuthenticationError, IdentityProviderUnavailable, Principal, TokenVerifier
from app.db.errors import RepositoryUnavailableError
from app.repositories.audit import AuditEntry, AuditRepository
from app.repositories.sync_history import SyncHistoryRepository
from app.repositories.operations import PostgresOperationsRepository
from app.repositories.tenants import TenantRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository
from app.services.account_sync import AccountSyncService
from app.webhooks.ingest import WebhookIngestor

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False, description="OIDC access token (RFC 9068 JWT)")


# --------------------------------------------------------------------------- #
# Services and repositories
# --------------------------------------------------------------------------- #
def get_engine() -> Engine:
    return wiring.engine()


def get_sync_service() -> AccountSyncService:
    return wiring.sync_service()


def get_sync_history() -> SyncHistoryRepository:
    return wiring.sync_history()


def get_tenant_repository() -> TenantRepository:
    return wiring.tenant_repository()


def get_audit_repository() -> AuditRepository:
    return wiring.audit_repository()


def get_token_verifier() -> TokenVerifier:
    return wiring.token_verifier()


def get_event_repository() -> PostgresWebhookEventRepository:
    return wiring.webhook_events()


def get_operations_repository() -> PostgresOperationsRepository:
    return wiring.operations_repository()


def get_webhook_ingestor() -> WebhookIngestor:
    return wiring.webhook_ingestor()


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def _unauthorized(description: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=description,
        headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{description}"'},
    )


def get_principal(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    tenants: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise HTTPException(status_code=401, detail="authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        principal = verifier.verify(creds.credentials)
    except IdentityProviderUnavailable as exc:
        logger.error("auth.idp_unavailable error=%s", exc)
        raise HTTPException(status_code=503, detail="identity provider unavailable") from exc
    except AuthenticationError as exc:
        logger.info("auth.denied reason=%s", exc)
        raise _unauthorized(str(exc)) from exc

    try:
        tenant = tenants.get_tenant(principal.tenant_id)
    except RepositoryUnavailableError as exc:
        raise HTTPException(status_code=503, detail="tenant directory unavailable") from exc
    if tenant is None:
        logger.warning("auth.denied reason=unknown_tenant sub=%s", principal.subject)
        raise HTTPException(status_code=403, detail="tenant is not provisioned")
    if not tenant.active:
        logger.warning("auth.denied reason=tenant_suspended tenant=%s sub=%s", tenant.id, principal.subject)
        raise HTTPException(status_code=403, detail="tenant is suspended")
    return principal


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #
def _forbidden(principal: Principal, permission: Permission) -> HTTPException:
    logger.info("auth.forbidden tenant=%s sub=%s needs=%s", principal.tenant_id, principal.subject,
                permission.value)
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"missing permission {permission.value}",
        headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{permission.value}"'},
    )


def require(permission: Permission, *, audit_action: str | None = None) -> Callable[..., Principal]:
    """Dependency factory: an authenticated principal that holds ``permission``.

    With ``audit_action``, a denial is also written to the audit log (best
    effort: an audit failure never turns the 403 into a 500).
    """
    if audit_action is None:
        def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
            if not principal.has(permission):
                raise _forbidden(principal, permission)
            return principal
    else:
        def dependency(principal: Annotated[Principal, Depends(get_principal)],
                       audit: Annotated[AuditRepository, Depends(get_audit_repository)]) -> Principal:
            if not principal.has(permission):
                try:
                    audit.record(AuditEntry(actor=principal.subject, actor_client_id=principal.client_id,
                                            tenant_id=principal.tenant_id, action=audit_action, outcome="denied",
                                            details={"missing_permission": permission.value,
                                                     "roles": sorted(r.value for r in principal.roles)}))
                except Exception as exc:  # noqa: BLE001
                    logger.error("audit.write_failed action=%s error=%s", audit_action, type(exc).__name__)
                raise _forbidden(principal, permission)
            return principal

    dependency.__name__ = f"require_{permission.name.lower()}" + ("_audited" if audit_action else "")
    return dependency
