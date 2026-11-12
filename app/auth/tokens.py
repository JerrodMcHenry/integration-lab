"""OIDC / OAuth 2.0 JWT access-token verification (RFC 7519, RFC 9068).

Checks, in order:
1. Well-formed JWT, header ``alg`` in the configured allowlist (never
   ``none`` or HS*), ``kid`` present, ``typ`` equal to ``at+jwt``.
2. Signature, using the key with that ``kid`` from the IdP's JWKS (fetched
   over HTTP and cached; refreshed on an unknown ``kid`` to follow key rotation,
   rate-limited so garbage tokens can't force JWKS fetches).
3. ``iss`` exact match, ``aud`` contains this API, ``exp``, ``nbf``, ``iat``
   (with small leeway), ``sub`` present, and lifetime (exp - iat) within limit.
4. Tenant claim present and well-formed. Roles claim is a list of known roles
   (unknown roles are ignored, not trusted).

Token contents are never logged.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from app.auth.permissions import Permission, Role, permissions_for
from app.core.config import OIDCSettings

logger = logging.getLogger(__name__)

_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MIN_REFRESH_INTERVAL_SECONDS = 30


class AuthenticationError(Exception):
    """The request is not authenticated (HTTP 401). Message is safe to return."""


class IdentityProviderUnavailable(AuthenticationError):
    """JWKS could not be fetched; tokens can't be verified (HTTP 503)."""


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[Role]
    permissions: frozenset[Permission]
    client_id: str | None
    token_id: str | None

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions


# --------------------------------------------------------------------------- #
# JWKS
# --------------------------------------------------------------------------- #
JWKSFetcher = Callable[[], dict[str, Any]]


def http_jwks_fetcher(settings: OIDCSettings, client: httpx.Client | None = None) -> JWKSFetcher:
    """Fetch the JWKS over HTTP, discovering ``jwks_uri`` when not configured."""
    http = client or httpx.Client(timeout=5.0)
    state: dict[str, str | None] = {"jwks_url": settings.jwks_url}

    def fetch() -> dict[str, Any]:
        try:
            if not state["jwks_url"]:
                disco = http.get(f"{settings.issuer}/.well-known/openid-configuration")
                disco.raise_for_status()
                meta = disco.json()
                if meta.get("issuer", "").rstrip("/") != settings.issuer:
                    raise IdentityProviderUnavailable("discovery document issuer mismatch")
                state["jwks_url"] = meta["jwks_uri"]
            response = http.get(state["jwks_url"])
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise IdentityProviderUnavailable(f"cannot fetch JWKS: {type(exc).__name__}") from exc

    return fetch


class JWKSCache:
    def __init__(self, fetcher: JWKSFetcher, *, ttl_seconds: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetcher
        self._ttl = ttl_seconds
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def get(self, kid: str) -> PyJWK:
        with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at > self._ttl
            if stale:
                self._refresh(now)
            elif kid not in self._keys and now - self._fetched_at >= _MIN_REFRESH_INTERVAL_SECONDS:
                self._refresh(now)  # possibly a newly rotated key
            key = self._keys.get(kid)
        if key is None:
            raise AuthenticationError("token signed with an unknown key")
        return key

    def _refresh(self, now: float) -> None:
        jwks = self._fetch()
        keys: dict[str, PyJWK] = {}
        for raw in jwks.get("keys", []):
            if raw.get("use", "sig") != "sig" or "kid" not in raw:
                continue
            try:
                keys[raw["kid"]] = PyJWK(raw)
            except jwt.PyJWTError:
                logger.warning("auth.jwks.skipped_key kid=%s", raw.get("kid"))
        self._keys = keys
        self._fetched_at = now
        logger.info("auth.jwks.refreshed keys=%d", len(keys))


# --------------------------------------------------------------------------- #
# Verifier
# --------------------------------------------------------------------------- #
class TokenVerifier:
    def __init__(self, settings: OIDCSettings, jwks: JWKSCache) -> None:
        self._s = settings
        self._jwks = jwks

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthenticationError("malformed token") from exc

        alg = header.get("alg")
        if alg not in self._s.algorithms:
            raise AuthenticationError("token algorithm not allowed")
        if self._s.required_token_type and str(header.get("typ", "")).lower() not in (
            self._s.required_token_type, f"application/{self._s.required_token_type}"
        ):
            raise AuthenticationError("token is not an access token")
        kid = header.get("kid")
        if not kid or not isinstance(kid, str):
            raise AuthenticationError("token has no key id")

        key = self._jwks.get(kid)
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],  # alg already allowlisted; also must match the key type
                audience=self._s.audience,
                issuer=self._s.issuer,
                leeway=self._s.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"], "verify_signature": True},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("token expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise AuthenticationError("token not yet valid") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError("token audience invalid") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError("token issuer invalid") from exc
        except jwt.InvalidSignatureError as exc:
            raise AuthenticationError("token signature invalid") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthenticationError(f"token missing required claim '{exc.claim}'") from exc
        except jwt.PyJWTError as exc:
            raise AuthenticationError("token invalid") from exc

        return self._principal(claims)

    def _principal(self, claims: dict[str, Any]) -> Principal:
        iat, exp = claims["iat"], claims["exp"]
        if not isinstance(iat, (int, float)) or iat > time.time() + self._s.leeway_seconds:
            raise AuthenticationError("token issued in the future")
        if exp - iat > self._s.max_token_lifetime_seconds:
            raise AuthenticationError("token lifetime exceeds policy")

        sub = claims["sub"]
        if not isinstance(sub, str) or not sub:
            raise AuthenticationError("token subject invalid")

        tenant = claims.get(self._s.tenant_claim)
        if not isinstance(tenant, str) or not _TENANT_ID.match(tenant):
            raise AuthenticationError("token has no valid tenant claim")

        raw_roles = claims.get(self._s.roles_claim, [])
        if not isinstance(raw_roles, list):
            raise AuthenticationError("token roles claim must be a list")
        # Unknown roles are ignored. A token with no known role is authenticated but
        # has no permissions, so every protected endpoint returns 403.
        roles = frozenset(Role(r) for r in raw_roles if r in Role._value2member_map_)

        return Principal(
            subject=sub,
            tenant_id=tenant,
            roles=roles,
            permissions=permissions_for(roles),
            client_id=claims.get("client_id") or claims.get("azp"),
            token_id=claims.get("jti"),
        )
