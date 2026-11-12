"""A test identity provider: real RSA keys, a JWKS, and signed RFC 9068 access tokens.

API tests use the production ``TokenVerifier`` against this IdP. Authentication
is never bypassed with a fake principal.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.auth.tokens import JWKSCache, TokenVerifier
from app.core.config import OIDCSettings

ISSUER = "https://idp.test"
AUDIENCE = "integrationlab-api"


def new_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class TestIdP:
    __test__ = False  # not a pytest test class

    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.active_kid = self.rotate()
        self.jwks_fetches = 0

    def rotate(self) -> str:
        kid = f"test-{uuid.uuid4().hex[:8]}"
        self.keys[kid] = new_rsa_key()
        self.active_kid = kid
        return kid

    def jwks(self) -> dict[str, Any]:
        self.jwks_fetches += 1
        keys = []
        for kid, key in self.keys.items():
            jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": keys}

    def settings(self, **overrides) -> OIDCSettings:
        values = dict(issuer=ISSUER, audience=AUDIENCE, leeway_seconds=5)
        values.update(overrides)
        return OIDCSettings(**values)

    def verifier(self, **settings_overrides) -> TokenVerifier:
        return TokenVerifier(self.settings(**settings_overrides), JWKSCache(self.jwks, ttl_seconds=600))

    def token(self, *, tenant: str = "meridian", roles: list[str] | None = None, sub: str | None = None,
              lifetime: int = 300, iat_offset: int = 0, headers: dict | None = None,
              key=None, kid: str | None = None, drop: tuple[str, ...] = (), **claims) -> str:
        now = int(time.time()) + iat_offset
        payload = {
            "iss": ISSUER, "aud": AUDIENCE, "sub": sub or f"user-{tenant}",
            "iat": now, "nbf": now, "exp": now + lifetime, "jti": uuid.uuid4().hex,
            "client_id": "integrationlab-tests", "tenant_id": tenant,
            "roles": ["operator"] if roles is None else roles,
        }
        payload.update(claims)
        for name in drop:
            payload.pop(name, None)
        kid = kid or self.active_kid
        hdr = {"kid": kid, "typ": "at+jwt", **(headers or {})}
        return jwt.encode(payload, key or self.keys[kid], algorithm=hdr.pop("alg", "RS256"), headers=hdr)

    def headers(self, role: str = "operator", tenant: str = "meridian", **kw) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(tenant=tenant, roles=[role], **kw)}"}
