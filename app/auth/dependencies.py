"""FastAPI dependencies for authentication and authorization.

Tenant identity comes **only** from the verified token's tenant claim, and it
must name an existing, active tenant row. Path, query, header and body values
are never consulted for tenant identity.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.permissions import Permission
from app.auth.tokens import (
    AuthenticationError,
    IdentityProviderUnavailable,
    JWKSCache,
    Principal,
    TokenVerifier,
    http_jwks_fetcher,
)
from app.core.config import get_settings
from app.repositories.sync_history import RepositoryUnavailableError
from app.repositories.tenants import TenantRepository

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False, description="OIDC access token (RFC 9068 JWT)")


@lru_cache
def get_token_verifier() -> TokenVerifier:
    oidc = get_settings().oidc
    return TokenVerifier(oidc, JWKSCache(http_jwks_fetcher(oidc), ttl_seconds=oidc.jwks_cache_seconds))


def get_tenant_repository() -> TenantRepository:
    # Late import avoids a cycle: app.api.deps builds services that depend on auth.
    from app.api.deps import get_tenant_repository as _get

    return _get()


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


def require(permission: Permission) -> Callable[..., Principal]:
    """Dependency factory: authenticated principal that holds ``permission``."""

    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not principal.has(permission):
            logger.info("auth.forbidden tenant=%s sub=%s needs=%s", principal.tenant_id,
                        principal.subject, permission.value)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing permission {permission.value}",
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{permission.value}"'},
            )
        return principal

    dependency.__name__ = f"require_{permission.name.lower()}"
    return dependency
