"""Mock OpenID Connect identity provider (development and tests only).

Implements the parts of OIDC / OAuth 2.0 that IntegrationLab consumes:

- ``GET /.well-known/openid-configuration``: discovery (OIDC Discovery 1.0)
- ``GET /jwks``: public signing keys (RFC 7517)
- ``GET/POST /authorize`` + ``POST /oauth/token`` (``authorization_code``): the
  browser flow for the dashboard (RFC 6749 + PKCE RFC 7636, S256 only). Public
  client ``integrationlab-dashboard`` (no secret), exact redirect-URI matching,
  one-time 60 s codes deleted on the first redemption attempt.
- ``POST /oauth/token``: RFC 9068 JWT access tokens (RS256, ``typ: at+jwt``), also via
  * ``password`` grant for named demo users (dev convenience only; this grant
    is deprecated in OAuth 2.1 and must not be used with a real IdP), and
  * ``client_credentials`` for tenant-bound service accounts.

Not implemented: ID tokens, userinfo, refresh tokens, consent, IdP sessions. IntegrationLab's verifier is IdP-agnostic, so a real
provider (Keycloak, Entra ID, Auth0, Okta) can replace this by configuration.

Refuses to start in production-like environments. Signing keys live in
memory and change on restart; ``/_admin/rotate-keys`` (dev only) rotates
while keeping the previous key published, to exercise key rotation.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import time
import uuid
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jwt.algorithms import RSAAlgorithm

from mock_common.guard import PRODUCTION_ENVS, admin_routes_enabled

ISSUER = os.getenv("MOCK_IDP_ISSUER", "http://localhost:8090").rstrip("/")
AUDIENCE = os.getenv("MOCK_IDP_AUDIENCE", "integrationlab-api")
TOKEN_TTL_SECONDS = int(os.getenv("MOCK_IDP_TOKEN_TTL_SECONDS", "900"))
DEV_PASSWORD = os.getenv("MOCK_IDP_DEV_PASSWORD", "dev-only-password")
PUBLIC_CLIENT_ID = "integrationlab-cli"

#: Browser client for the dashboard: public (no secret), authorization code + PKCE only.
DASHBOARD_CLIENT_ID = "integrationlab-dashboard"
#: Exact-match allow-list; nothing else is ever redirected to.
DASHBOARD_REDIRECT_URIS = tuple(
    u.strip() for u in os.getenv("MOCK_IDP_DASHBOARD_REDIRECT_URIS",
                                 "http://localhost:5173/callback,http://localhost:8080/callback").split(",")
    if u.strip())
AUTH_CODE_TTL_SECONDS = 60
_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")   # RFC 7636 §4.1
_S256_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")     # base64url(SHA-256), unpadded
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_LOGIN_PAGE_HEADERS = {**_NO_STORE, "X-Frame-Options": "DENY",
                       "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}

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


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def _login_page(params: dict[str, str], error: str | None = None) -> str:
    e = html.escape
    hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in params.items())
    options = "".join(f'<option value="{e(u)}">{e(u)}: {e(t)} / {e(", ".join(r) or "no role")}</option>'
                      for u, (t, r) in DEMO_USERS.items())
    banner = f'<p class="err" role="alert">{e(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in · IntegrationLab (mock IdP)</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f6f9;margin:0;display:grid;place-items:center;min-height:100vh}}
form{{background:#fff;padding:2rem;border-radius:12px;box-shadow:0 4px 24px #0001;width:min(360px,90vw)}}
label{{display:block;margin:.8rem 0 .3rem;font-weight:600}}select,input{{width:100%;padding:.55rem;box-sizing:border-box}}
button{{margin-top:1.2rem;width:100%;padding:.7rem;background:#1f4fd1;color:#fff;border:0;border-radius:6px;font-size:1rem}}
.note{{color:#667;font-size:.85rem}}.err{{color:#b42318}}</style></head><body>
<form method="post" action=""><h1>Sign in</h1><p class="note">Development identity provider. Never use in production.</p>
{banner}{hidden}<label for="u">Demo user</label><select id="u" name="username">{options}</select>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="off" required>
<p class="note">Development password: <code>dev-only-password</code></p><button type="submit">Sign in</button></form></body></html>"""


def create_app(*, app_env: str | None = None, issuer: str = ISSUER, audience: str = AUDIENCE,
               token_ttl: int = TOKEN_TTL_SECONDS, admin_enabled: bool | None = None,
               redirect_uris: tuple[str, ...] = DASHBOARD_REDIRECT_URIS,
               clock: Callable[[], float] = time.time) -> FastAPI:
    env = (app_env if app_env is not None else os.getenv("APP_ENV", "development")).strip().lower()
    if env in PRODUCTION_ENVS:
        raise UnsafeIdPConfiguration(
            f"The mock identity provider must never run in a production-like environment (APP_ENV={env!r}).")
    enable_admin = admin_routes_enabled(env) if admin_enabled is None else admin_enabled

    app = FastAPI(title="IntegrationLab Mock IdP")
    ring = KeyRing()
    app.state.keyring = ring
    basic = HTTPBasic(auto_error=False)
    #: code -> {client_id, redirect_uri, challenge, user, expires}
    codes: dict[str, dict] = {}
    app.state.auth_codes = codes

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
            "authorization_endpoint": f"{issuer}/authorize",
            "grant_types_supported": ["authorization_code", "password", "client_credentials"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
            "claims_supported": ["sub", "iss", "aud", "exp", "iat", "tenant_id", "roles"],
        }

    @app.get("/jwks")
    def jwks():
        return ring.jwks()

    # -- authorization code + PKCE (browser) -------------------------------- #
    def authorize_params(src: dict[str, str]) -> dict[str, str]:
        """Validate an authorization request. Errors render a page; they never redirect."""
        if src.get("client_id") != DASHBOARD_CLIENT_ID:
            raise HTTPException(400, detail="unknown client_id")
        if src.get("redirect_uri") not in redirect_uris:  # exact match, no prefixes or wildcards
            raise HTTPException(400, detail="redirect_uri is not registered for this client")
        if src.get("response_type") != "code":
            raise HTTPException(400, detail="response_type must be 'code'")
        if src.get("code_challenge_method") != "S256":
            raise HTTPException(400, detail="code_challenge_method must be S256")
        if not _S256_CHALLENGE.match(src.get("code_challenge", "")):
            raise HTTPException(400, detail="code_challenge must be a base64url SHA-256 value")
        state = src.get("state", "")
        if not 16 <= len(state) <= 256:
            raise HTTPException(400, detail="state is required (16-256 characters)")
        keys = ("client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method", "state")
        return {k: src[k] for k in keys}

    @app.get("/authorize", response_class=HTMLResponse)
    def authorize_form(request: Request):
        params = authorize_params(dict(request.query_params))
        return HTMLResponse(_login_page(params), headers=_LOGIN_PAGE_HEADERS)

    @app.post("/authorize")
    async def authorize_submit(request: Request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        params = authorize_params(form)
        user, password = form.get("username", ""), form.get("password", "")
        if user not in DEMO_USERS or not secrets.compare_digest(password.encode(), DEV_PASSWORD.encode()):
            return HTMLResponse(_login_page(params, "Invalid username or password."), status_code=401,
                                headers=_LOGIN_PAGE_HEADERS)
        now = clock()
        for stale in [c for c, v in codes.items() if v["expires"] <= now]:
            del codes[stale]
        code = secrets.token_urlsafe(32)
        codes[code] = {"client_id": params["client_id"], "redirect_uri": params["redirect_uri"],
                       "challenge": params["code_challenge"], "user": user, "expires": now + AUTH_CODE_TTL_SECONDS}
        query = urlencode({"code": code, "state": params["state"]})
        return RedirectResponse(f"{params['redirect_uri']}?{query}", status_code=302, headers=_NO_STORE)

    @app.post("/oauth/token")
    async def token(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        grant = form.get("grant_type")
        scope = form.get("scope", "integrationlab")

        if grant == "authorization_code":
            # One-time: removed on the first redemption attempt, whether or not it succeeds.
            entry = codes.pop(form.get("code", ""), None)
            if (entry is None or entry["expires"] <= clock()
                    or form.get("client_id") != entry["client_id"]
                    or form.get("redirect_uri") != entry["redirect_uri"]
                    or not _VERIFIER.match(form.get("code_verifier", ""))
                    or not secrets.compare_digest(s256(form["code_verifier"]), entry["challenge"])):
                return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_NO_STORE)
            tenant, roles = DEMO_USERS[entry["user"]]
            return JSONResponse(issue(entry["user"], tenant, roles, DASHBOARD_CLIENT_ID, scope), headers=_NO_STORE)

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
