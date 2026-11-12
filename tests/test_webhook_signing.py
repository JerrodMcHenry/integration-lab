"""Webhook signature scheme: verification, replay window, header parsing, interoperability."""

from __future__ import annotations

import pytest

from app.webhooks.signing import (
    SignatureError,
    TimestampOutsideTolerance,
    compute_signature,
    parse_header,
    sign_header,
    verify,
)
from mock_crm.main import sign_webhook as mock_crm_sign

SECRET = "whsec_test_meridian"
BODY = b'{"event_id":"evt_1","event_type":"account.updated","data":{"account_id":"SF-1001"}}'
NOW = 1_800_000_000


def check(header, body=BODY, secrets=(SECRET,), now=NOW, tolerance=300):
    return verify(header, body, list(secrets), tolerance_seconds=tolerance, clock=lambda: now)


def test_valid_signature_returns_signed_timestamp():
    assert check(sign_header(SECRET, BODY, NOW)) == NOW


def test_signature_is_over_exact_raw_bytes():
    header = sign_header(SECRET, BODY, NOW)
    reformatted = b'{"event_id": "evt_1", "event_type": "account.updated", "data": {"account_id": "SF-1001"}}'
    with pytest.raises(SignatureError):
        check(header, reformatted)  # same JSON meaning, different bytes -> rejected


@pytest.mark.parametrize("mutate", [
    lambda b: b.replace(b"SF-1001", b"SF-1002"),       # retarget the account
    lambda b: b + b" ",
    lambda b: b[:-1],
])
def test_tampered_body_rejected(mutate):
    with pytest.raises(SignatureError):
        check(sign_header(SECRET, BODY, NOW), mutate(BODY))


def test_wrong_secret_rejected():
    with pytest.raises(SignatureError):
        check(sign_header("other-tenant-secret", BODY, NOW))


def test_timestamp_is_signed_and_cannot_be_moved():
    sig = compute_signature(SECRET, NOW - 3600, BODY)  # captured old delivery
    with pytest.raises(SignatureError):
        check(f"t={NOW},v1={sig}")  # attacker refreshes t without the secret


@pytest.mark.parametrize("age", [301, 3600, -301])
def test_outside_tolerance_rejected(age):
    with pytest.raises(TimestampOutsideTolerance):
        check(sign_header(SECRET, BODY, NOW - age))


@pytest.mark.parametrize("age", [0, 299, -299])
def test_inside_tolerance_accepted(age):
    check(sign_header(SECRET, BODY, NOW - age))


def test_window_checked_only_after_signature():
    """An unauthenticated caller can't learn anything about the replay window."""
    with pytest.raises(SignatureError) as exc_info:
        check(sign_header("wrong", BODY, NOW - 10_000))
    assert not isinstance(exc_info.value, TimestampOutsideTolerance)


def test_rotation_previous_secret_and_multiple_v1():
    assert check(sign_header("old-secret", BODY, NOW), secrets=(SECRET, "old-secret")) == NOW
    both = f"t={NOW},v1={compute_signature('bogus', NOW, BODY)},v1={compute_signature(SECRET, NOW, BODY)}"
    assert check(both) == NOW


@pytest.mark.parametrize("header", [
    None, "", "garbage", "t=,v1=abc", f"t={NOW}", "v1=" + "a" * 64, f"t=abc,v1={'a'*64}",
    f"t={NOW},v1=XYZ", f"t={NOW},v1={'a'*63}", f"t={NOW},t={NOW},v1={'a'*64}",
    f"t={NOW}," + ",".join(f"v1={'a'*64}" for _ in range(6)), "t=1," + "x" * 2000,
])
def test_malformed_headers(header):
    with pytest.raises(SignatureError):
        check(header)


def test_unknown_scheme_ignored_for_forward_compat():
    header = f"t={NOW},v0=whatever,v1={compute_signature(SECRET, NOW, BODY)}"
    assert parse_header(header)[0] == NOW
    check(header)


def test_no_secrets_configured_fails_closed():
    with pytest.raises(SignatureError):
        check(sign_header(SECRET, BODY, NOW), secrets=())


def test_mock_crm_independent_signer_interoperates():
    assert check(mock_crm_sign(SECRET, BODY, NOW)) == NOW
