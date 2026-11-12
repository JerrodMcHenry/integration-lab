"""Mock OpenID Connect identity provider (development and tests only).

Implements the parts of OIDC / OAuth 2.0 that IntegrationLab consumes:

- ``GET /.well-known/openid-configuration``: discovery (OIDC Discovery 1.0)
- ``GET /jwks``: public signing keys (RFC 7517)
- ``POST /oauth/token``: RFC 9068 JWT access tokens (RS256, ``typ: at+jwt``) via
  * ``password`` grant for named demo users (dev convenience only; this grant
    is deprecated in OAuth 2.1 and must not be used with a real IdP), and
  * ``client_credentials`` for tenant-bound service accounts.

Not implemented: authorization code + PKCE, ID tokens, userinfo, refresh
tokens, consent. IntegrationLab's verifier is IdP-agnostic, so a real
provider (Keycloak, Entra ID, Auth0, Okta) can replace this by configuration.

Refuses to start in production-like environments. Signing keys live in
memory and change on restart; ``/_admin/rotate-keys`` (dev only) rotates
while keeping the previous key published, to exercise key rotation.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jwt.algorithms import RSAAlgorithm

from mock_common.guard import PRODUCTION_ENVS, admin_routes_enabled

ISSUER = os.getenv("MOCK_IDP_ISSUER", "http://localhost:8090").rstrip("/")
AUDIENCE = os.getenv("MOCK_IDP_AUDIENCE", "integrationlab-api")
TOKEN_TTL_SECONDS = int(os.getenv("MOCK_IDP_TOKEN_TTL_SECONDS", "900"))
DEV_PASSWORD = os.getenv("MOCK_IDP_DEV_PASSWORD", "dev-only-password")
PUBLIC_CLIENT_ID = "integrationlab-cli"

#: username -> (tenant, roles)
DEMO_USERS: dict[str, tuple[str, list[str]]] = {
    "meridian-admin": ("meridian", ["admin"]),
    "meridian-operator": ("meridian", ["operator"]),
    "meridian-viewer": ("meridian", ["viewer"]),
    "meridian-norole": ("meridian", []),
    "harbor-admin": ("harbor", ["admin"]),
    "harbor-operator": ("harbor", ["operator"]),
}

#: client_id -> (secret, tenant, roles) for client_credentials service accounts
SERVICE_CLIENTS: dict[str, tuple[str, str, list[str]]] = {
    "meridian-automation": (os.getenv("MOCK_IDP_MERIDIAN_AUTOMATION_SECRET", "dev-only-automation-secret"),
                            "meridian", ["operator"]),
}


class UnsafeIdPConfiguration(RuntimeError):
    pass


class KeyRing:
    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.order: list[str] = []
        self.rotate()

    def rotate(self) -> str:
        kid = f"mock-{uuid.uuid4().hex[:12]}"
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.order.append(kid)
        # Publish the current and previous key only.
        while len(self.order) > 2:
            del self.keys[self.order.pop(0)]
        return kid

    @property
    def active_kid(self) -> str:
        return self.order[-1]

    def jwks(self) -> dict:
        out = []
        for kid in self.order:
            jwk = RSAAlgorithm.to_jwk(self.keys[kid].public_key(), as_dict=True)
            out.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": out}


def create_app(*, app_env: str | None = None, issuer: str = ISSUER, audience: str = AUDIENCE,
               token_ttl: int = TOKEN_TTL_SECONDS, admin_enabled: bool | None = None) -> FastAPI:
    env = (app_env if app_env is not None else os.getenv("APP_ENV", "development")).strip().lower()
    if env in PRODUCTION_ENVS:
        raise UnsafeIdPConfiguration(
            f"The mock identity provider must never run in a production-like environment (APP_ENV={env!r}).")
    enable_admin = admin_routes_enabled(env) if admin_enabled is None else admin_enabled

    app = FastAPI(title="IntegrationLab Mock IdP")
    ring = KeyRing()
    app.state.keyring = ring
    basic = HTTPBasic(auto_error=False)

    def issue(sub: str, tenant: str, roles: list[str], client_id: str, scope: str) -> dict:
        now = int(time.time())
        claims = {"iss": issuer, "aud": audience, "sub": sub, "iat": now, "nbf": now,
                  "exp": now + token_ttl, "jti": uuid.uuid4().hex, "client_id": client_id,
                  "tenant_id": tenant, "roles": roles, "scope": scope}
        token = jwt.encode(claims, ring.keys[ring.active_kid], algorithm="RS256",
                           headers={"kid": ring.active_kid, "typ": "at+jwt"})
        return {"access_token": token, "token_type": "Bearer", "expires_in": token_ttl, "scope": scope}

    @app.get("/.well-known/openid-configuration")
    def discovery():
        return {
            "issuer": issuer,
            "token_endpoint": f"{issuer}/oauth/token",
            "jwks_uri": f"{issuer}/jwks",
            "grant_types_supported": ["password", "client_credentials"],
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
            "claims_supported": ["sub", "iss", "aud", "exp", "iat", "tenant_id", "roles"],
        }

    @app.get("/jwks")
    def jwks():
        return ring.jwks()

    @app.post("/oauth/token")
    async def token(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        grant = form.get("grant_type")
        scope = form.get("scope", "integrationlab")

        if grant == "password":
            if form.get("client_id") != PUBLIC_CLIENT_ID:
                raise HTTPException(401, detail={"error": "invalid_client"})
            user = form.get("username", "")
            password = form.get("password", "")
            if user not in DEMO_USERS or not secrets.compare_digest(password.encode(), DEV_PASSWORD.encode()):
                raise HTTPException(400, detail={"error": "invalid_grant"})
            tenant, roles = DEMO_USERS[user]
            return issue(user, tenant, roles, PUBLIC_CLIENT_ID, scope)

        if grant == "client_credentials":
            client = SERVICE_CLIENTS.get(creds.username) if creds else None
            if client is None or not secrets.compare_digest(creds.password.encode(), client[0].encode()):
                raise HTTPException(401, detail={"error": "invalid_client"})
            _, tenant, roles = client
            return issue(f"service:{creds.username}", tenant, roles, creds.username, scope)

        raise HTTPException(400, detail={"error": "unsupported_grant_type"})

    if enable_admin:
        admin = APIRouter(prefix="/_admin", tags=["dev-admin"])

        @admin.post("/rotate-keys")
        def rotate():
            return {"active_kid": ring.rotate(), "published": ring.order}

        app.include_router(admin)

    return app


app = create_app()
