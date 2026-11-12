"""Mock IdP authorization code + PKCE (S256) flow used by the dashboard."""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.auth.permissions import Role
from app.auth.tokens import JWKSCache, TokenVerifier, http_jwks_fetcher
from mock_idp import main as mock_idp
from tests.support.identity import TestIdP

ISSUER = "http://idp.local"
REDIRECT = "http://localhost:5173/callback"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def idp(clock):
    app = mock_idp.create_app(app_env="test", issuer=ISSUER, clock=clock)
    with TestClient(app, base_url=ISSUER, follow_redirects=False) as tc:
        yield tc


def pkce():
    verifier = secrets.token_urlsafe(48)  # 64 chars, within RFC 7636's 43-128
    return verifier, mock_idp.s256(verifier)


def auth_params(challenge, **over):
    p = {"response_type": "code", "client_id": mock_idp.DASHBOARD_CLIENT_ID, "redirect_uri": REDIRECT,
         "code_challenge": challenge, "code_challenge_method": "S256", "state": secrets.token_urlsafe(24)}
    p.update(over)
    return p


def login(idp, params, user="meridian-operator", password=mock_idp.DEV_PASSWORD):
    return idp.post("/authorize", data={**params, "username": user, "password": password})


def code_from(response):
    assert response.status_code == 302
    q = parse_qs(urlparse(response.headers["location"]).query)
    return q["code"][0], q["state"][0]


def exchange(idp, code, verifier, **over):
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": mock_idp.DASHBOARD_CLIENT_ID, "redirect_uri": REDIRECT}
    form.update(over)
    return idp.post("/oauth/token", data=form)


def test_full_flow_issues_a_token_the_api_accepts(idp):
    verifier, challenge = pkce()
    params = auth_params(challenge)
    page = idp.get("/authorize", params=params)
    assert page.status_code == 200 and "meridian-operator" in page.text
    assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"

    code, state = code_from(login(idp, params))
    assert state == params["state"]  # echoed for the browser's state check
    r = exchange(idp, code, verifier)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"

    settings = TestIdP().settings(issuer=ISSUER, audience=mock_idp.AUDIENCE)
    verifier_ = TokenVerifier(settings, JWKSCache(http_jwks_fetcher(settings, idp), ttl_seconds=600))
    p = verifier_.verify(r.json()["access_token"])
    assert (p.subject, p.tenant_id, p.roles, p.client_id) == (
        "meridian-operator", "meridian", frozenset({Role.OPERATOR}), mock_idp.DASHBOARD_CLIENT_ID)


def test_discovery_advertises_code_flow_with_s256_only(idp):
    d = idp.get("/.well-known/openid-configuration").json()
    assert d["authorization_endpoint"] == f"{ISSUER}/authorize"
    assert d["code_challenge_methods_supported"] == ["S256"] and "authorization_code" in d["grant_types_supported"]


# --- token endpoint: PKCE and one-time codes --------------------------------- #
def test_wrong_verifier_rejected_and_code_burned(idp):
    verifier, challenge = pkce()
    code, _ = code_from(login(idp, auth_params(challenge)))
    assert exchange(idp, code, pkce()[0]).json() == {"error": "invalid_grant"}
    assert exchange(idp, code, verifier).status_code == 400  # burned by the failed attempt


def test_code_is_single_use(idp):
    verifier, challenge = pkce()
    code, _ = code_from(login(idp, auth_params(challenge)))
    assert exchange(idp, code, verifier).status_code == 200
    assert exchange(idp, code, verifier).json() == {"error": "invalid_grant"}


def test_code_expires_after_60_seconds(idp, clock):
    verifier, challenge = pkce()
    code, _ = code_from(login(idp, auth_params(challenge)))
    clock.t += 60
    assert exchange(idp, code, verifier).json() == {"error": "invalid_grant"}


@pytest.mark.parametrize("override", [
    {"redirect_uri": "http://localhost:8080/callback"},     # registered, but not the one the code was issued for
    {"redirect_uri": REDIRECT + "/"},                        # not an exact match
    {"client_id": "integrationlab-cli"},
    {"code_verifier": "short"},                              # violates RFC 7636 length
])
def test_token_request_must_match_the_authorization(idp, override):
    verifier, challenge = pkce()
    code, _ = code_from(login(idp, auth_params(challenge)))
    assert exchange(idp, code, verifier, **override).json() == {"error": "invalid_grant"}


def test_unknown_code_rejected(idp):
    assert exchange(idp, "never-issued", pkce()[0]).json() == {"error": "invalid_grant"}


# --- authorize endpoint: never redirect to an unvalidated target --------------- #
@pytest.mark.parametrize("override, message", [
    ({"redirect_uri": "https://evil.example/callback"}, "redirect_uri is not registered"),
    ({"redirect_uri": "http://localhost:5173/callback?x=1"}, "redirect_uri is not registered"),
    ({"redirect_uri": "http://localhost:5173/"}, "redirect_uri is not registered"),
    ({"client_id": "someone-else"}, "unknown client_id"),
    ({"response_type": "token"}, "response_type"),
    ({"code_challenge_method": "plain"}, "S256"),
    ({"code_challenge": "too-short"}, "code_challenge"),
    ({"state": ""}, "state is required"),
    ({"state": "short"}, "state is required"),
])
def test_invalid_authorization_requests_are_rejected_without_redirect(idp, override, message):
    params = auth_params(pkce()[1], **override)
    for r in (idp.get("/authorize", params=params), login(idp, params)):
        assert r.status_code == 400 and "location" not in r.headers
        assert message in r.text


def test_wrong_password_redisplays_form_without_issuing_a_code(idp):
    params = auth_params(pkce()[1])
    r = login(idp, params, password="wrong")
    assert r.status_code == 401 and "location" not in r.headers and "Invalid username or password" in r.text
    assert idp.app.state.auth_codes == {}


def test_reflected_parameters_are_html_escaped(idp):
    params = auth_params(pkce()[1], state='"><script>alert(1)</script>xxxxxxxx')
    page = idp.get("/authorize", params=params).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_dashboard_client_has_no_secret_and_cannot_use_password_grant(idp):
    r = idp.post("/oauth/token", data={"grant_type": "password", "client_id": mock_idp.DASHBOARD_CLIENT_ID,
                                       "username": "meridian-admin", "password": mock_idp.DEV_PASSWORD})
    assert r.status_code == 401
    assert not hasattr(mock_idp, "DASHBOARD_CLIENT_SECRET")
