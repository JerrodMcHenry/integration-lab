"""TokenVerifier: signature, issuer, audience, expiry and claim validation (negative-heavy)."""

from __future__ import annotations

import base64
import json

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth.permissions import ROLE_PERMISSIONS, Permission, Role
from app.auth.tokens import (
    AuthenticationError,
    IdentityProviderUnavailable,
    JWKSCache,
    TokenVerifier,
    http_jwks_fetcher,
)
from mock_idp import main as mock_idp
from tests.support.identity import AUDIENCE, ISSUER, TestIdP, new_rsa_key


@pytest.fixture
def verifier(idp):
    return idp.verifier()


def reject(verifier, token, message):
    with pytest.raises(AuthenticationError) as exc_info:
        verifier.verify(token)
    assert message in str(exc_info.value)


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


# --- valid ------------------------------------------------------------------ #
def test_valid_token_yields_tenant_scoped_principal(verifier, idp):
    p = verifier.verify(idp.token(tenant="harbor", roles=["viewer"], sub="alice"))
    assert (p.subject, p.tenant_id, p.roles) == ("alice", "harbor", frozenset({Role.VIEWER}))
    assert p.permissions == ROLE_PERMISSIONS[Role.VIEWER]
    assert p.client_id == "integrationlab-tests" and p.token_id


# --- time-based claims -------------------------------------------------------- #
def test_expired_token(verifier, idp):
    reject(verifier, idp.token(iat_offset=-400, lifetime=300), "token expired")


def test_expiry_leeway_is_small(verifier, idp):
    verifier.verify(idp.token(iat_offset=-303, lifetime=300))  # 3 s past exp, within 5 s leeway
    reject(verifier, idp.token(iat_offset=-310, lifetime=300), "token expired")


def test_not_yet_valid(verifier, idp):
    reject(verifier, idp.token(nbf=9_999_999_999), "not yet valid")


def test_issued_in_future(verifier, idp):
    # PyJWT rejects a future iat itself ("not yet valid"); our own check is a second layer.
    with pytest.raises(AuthenticationError, match="not yet valid|issued in the future"):
        verifier.verify(idp.token(iat_offset=600))


def test_lifetime_over_policy(verifier, idp):
    reject(verifier, idp.token(lifetime=7200), "lifetime exceeds policy")


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "iss", "aud"])
def test_missing_required_claim(verifier, idp, claim):
    with pytest.raises(AuthenticationError):
        verifier.verify(idp.token(drop=(claim,)))


# --- issuer / audience --------------------------------------------------------- #
def test_wrong_issuer(verifier, idp):
    reject(verifier, idp.token(iss="https://evil.example"), "issuer invalid")


def test_wrong_audience(verifier, idp):
    reject(verifier, idp.token(aud="some-other-api"), "audience invalid")


def test_audience_list_containing_ours_is_accepted(verifier, idp):
    verifier.verify(idp.token(aud=["other-api", AUDIENCE]))


# --- signature and header attacks ------------------------------------------------ #
def test_signed_by_attacker_key_with_real_kid(verifier, idp):
    reject(verifier, idp.token(key=new_rsa_key()), "signature invalid")


def test_tampered_payload_tenant_escalation(verifier, idp):
    header, _payload, sig = idp.token(tenant="harbor", roles=["viewer"]).split(".")
    forged = jwt.decode(idp.token(tenant="harbor", roles=["viewer"]), options={"verify_signature": False})
    forged.update(tenant_id="meridian", roles=["admin"])
    reject(verifier, f"{header}.{b64(forged)}.{sig}", "signature invalid")


def test_alg_none_rejected(verifier, idp):
    claims = jwt.decode(idp.token(), options={"verify_signature": False})
    token = f"{b64({'alg': 'none', 'typ': 'at+jwt', 'kid': idp.active_kid})}.{b64(claims)}."
    reject(verifier, token, "algorithm not allowed")


def test_hs256_key_confusion_rejected(verifier, idp):
    """Classic attack: sign with HS256 using the RSA *public* key as the HMAC secret."""
    claims = jwt.decode(idp.token(roles=["admin"]), options={"verify_signature": False})
    public_pem = idp.keys[idp.active_kid].public_key().public_bytes(
        encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.PEM,
        format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).PublicFormat.SubjectPublicKeyInfo)
    header = {"alg": "HS256", "typ": "at+jwt", "kid": idp.active_kid}
    signing_input = f"{b64(header)}.{b64(claims)}".encode()
    import hashlib
    import hmac
    sig = base64.urlsafe_b64encode(hmac.new(public_pem, signing_input, hashlib.sha256).digest()).rstrip(b"=")
    reject(verifier, f"{signing_input.decode()}.{sig.decode()}", "algorithm not allowed")


def test_id_token_or_plain_jwt_not_accepted_as_access_token(verifier, idp):
    reject(verifier, idp.token(headers={"typ": "JWT"}), "not an access token")


def test_application_at_jwt_typ_accepted(verifier, idp):
    verifier.verify(idp.token(headers={"typ": "application/at+jwt"}))


def test_missing_kid(verifier, idp):
    token = jwt.encode(jwt.decode(idp.token(), options={"verify_signature": False}),
                       idp.keys[idp.active_kid], algorithm="RS256", headers={"typ": "at+jwt"})
    reject(verifier, token, "no key id")


def test_unknown_kid(verifier, idp):
    reject(verifier, idp.token(kid="nope", key=idp.keys[idp.active_kid]), "unknown key")


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "Bearer x.y.z"])
def test_malformed_tokens(verifier, garbage):
    with pytest.raises(AuthenticationError):
        verifier.verify(garbage)


# --- custom claims ------------------------------------------------------------- #
@pytest.mark.parametrize("tenant", [None, "", "Meridian", "../meridian", "a" * 64, 123])
def test_invalid_tenant_claim(verifier, idp, tenant):
    token = idp.token(drop=("tenant_id",)) if tenant is None else idp.token(tenant_id=tenant)
    reject(verifier, token, "tenant claim")


def test_roles_must_be_a_list(verifier, idp):
    reject(verifier, idp.token(roles="admin"), "roles claim must be a list")


def test_unknown_roles_ignored_not_trusted(verifier, idp):
    p = verifier.verify(idp.token(roles=["superuser", "root", "viewer"]))
    assert p.roles == frozenset({Role.VIEWER})


def test_no_known_role_means_no_permissions(verifier, idp):
    assert verifier.verify(idp.token(roles=[])).permissions == frozenset()


# --- JWKS caching and rotation --------------------------------------------------- #
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_key_rotation_picked_up_after_refresh_interval():
    idp = TestIdP()
    clock = Clock()
    v = TokenVerifier(idp.settings(), JWKSCache(idp.jwks, ttl_seconds=600, clock=clock))
    v.verify(idp.token())
    idp.rotate()
    new_token = idp.token()
    with pytest.raises(AuthenticationError, match="unknown key"):
        v.verify(new_token)  # within 30 s of the last fetch: no refetch
    clock.t += 31
    assert v.verify(new_token).tenant_id == "meridian"


def test_unknown_kid_flood_cannot_force_jwks_refetches():
    idp = TestIdP()
    v = TokenVerifier(idp.settings(), JWKSCache(idp.jwks, ttl_seconds=600, clock=Clock()))
    v.verify(idp.token())
    before = idp.jwks_fetches
    for i in range(50):
        with pytest.raises(AuthenticationError):
            v.verify(idp.token(kid=f"bogus-{i}", key=idp.keys[idp.active_kid]))
    assert idp.jwks_fetches == before


def test_idp_unreachable_raises_unavailable(idp):
    def down():
        raise IdentityProviderUnavailable("cannot fetch JWKS")

    v = TokenVerifier(idp.settings(), JWKSCache(down, ttl_seconds=600))
    with pytest.raises(IdentityProviderUnavailable):
        v.verify(idp.token())


# --- against the mock IdP (discovery + JWKS over HTTP) ------------------------------ #
def test_discovery_jwks_and_password_grant_with_mock_idp():
    app = mock_idp.create_app(app_env="test", issuer="http://idp.local")
    with TestClient(app, base_url="http://idp.local") as tc:
        settings = TestIdP().settings(issuer="http://idp.local", audience=mock_idp.AUDIENCE)
        v = TokenVerifier(settings, JWKSCache(http_jwks_fetcher(settings, tc), ttl_seconds=600))
        r = tc.post("/oauth/token", data={"grant_type": "password", "client_id": "integrationlab-cli",
                                          "username": "harbor-operator", "password": mock_idp.DEV_PASSWORD})
        p = v.verify(r.json()["access_token"])
        assert (p.tenant_id, p.roles) == ("harbor", frozenset({Role.OPERATOR}))


def test_discovery_issuer_mismatch_is_rejected():
    app = mock_idp.create_app(app_env="test", issuer="http://evil.local")
    with TestClient(app, base_url="http://idp.local") as tc:
        settings = TestIdP().settings(issuer="http://idp.local")
        with pytest.raises(IdentityProviderUnavailable, match="issuer mismatch"):
            http_jwks_fetcher(settings, tc)()


def test_jwks_http_failure_is_unavailable():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    settings = TestIdP().settings(jwks_url="http://idp.local/jwks")
    with pytest.raises(IdentityProviderUnavailable):
        http_jwks_fetcher(settings, client)()


# --- RBAC matrix ---------------------------------------------------------------- #
def test_role_permission_matrix():
    assert ROLE_PERMISSIONS[Role.VIEWER] == {Permission.TENANT_READ, Permission.HISTORY_READ}
    assert ROLE_PERMISSIONS[Role.OPERATOR] == ROLE_PERMISSIONS[Role.VIEWER] | {Permission.SYNC_RUN}
    assert ROLE_PERMISSIONS[Role.ADMIN] == ROLE_PERMISSIONS[Role.OPERATOR] | {
        Permission.CONFIG_READ, Permission.CONFIG_WRITE}


def test_settings_reject_symmetric_or_none_algorithms():
    for algs in (("HS256",), ("none",), ("RS256", "HS512")):
        with pytest.raises(ValueError):
            TestIdP().settings(algorithms=algs)


def test_issuer_constant():
    assert ISSUER.startswith("https://")
