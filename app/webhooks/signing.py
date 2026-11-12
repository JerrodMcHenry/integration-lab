"""Webhook signature verification.

Header (sent by the CRM):

    X-IntegrationLab-Signature: t=1731390000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd

``v1 = hex(HMAC-SHA256(secret, f"{t}.{raw_body}"))``, computed over the exact
bytes received, before any JSON parsing. Several ``v1=`` entries may be sent
during secret rotation; one valid match is enough. The verifier also accepts
a second (previous) secret for zero-downtime rotation.

Replay protection is two-layered:
1. ``t`` is part of the signed message and must be within ``tolerance_seconds``
   of the receiver's clock (it can't be altered without the secret).
2. Inside the window, a byte-identical replay carries the same ``event_id``
   and is deduplicated by a tenant-scoped unique constraint.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Sequence

SIGNATURE_HEADER = "X-IntegrationLab-Signature"
_MAX_SIGNATURES = 5


class SignatureError(Exception):
    """Verification failed. Messages are generic so they don't help an attacker."""


class TimestampOutsideTolerance(SignatureError):
    pass


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    t = int(time.time()) if timestamp is None else timestamp
    return f"t={t},v1={compute_signature(secret, t, body)}"


def parse_header(header: str | None) -> tuple[int, list[str]]:
    if not header or len(header) > 1024:
        raise SignatureError("missing or malformed signature header")
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            raise SignatureError("missing or malformed signature header")
        if key == "t":
            if timestamp is not None or not value.isdigit() or len(value) > 12:
                raise SignatureError("missing or malformed signature header")
            timestamp = int(value)
        elif key == "v1":
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise SignatureError("missing or malformed signature header")
            signatures.append(value)
        # Unknown schemes (v0, v2, ...) are ignored for forward compatibility.
    if timestamp is None or not signatures or len(signatures) > _MAX_SIGNATURES:
        raise SignatureError("missing or malformed signature header")
    return timestamp, signatures


def verify(header: str | None, body: bytes, secrets: Sequence[str], *, tolerance_seconds: int,
           clock: Callable[[], float] = time.time) -> int:
    """Return the signed timestamp if valid, otherwise raise ``SignatureError``.

    The signature is checked before the timestamp window, so the window
    response is only ever given to someone who holds the secret.
    """
    timestamp, provided = parse_header(header)
    expected = [compute_signature(s, timestamp, body) for s in secrets if s]
    if not expected:
        raise SignatureError("signature verification failed")
    # Compare every pair in constant time (no early exit that leaks which matched).
    matched = False
    for exp in expected:
        for got in provided:
            matched |= hmac.compare_digest(exp, got)
    if not matched:
        raise SignatureError("signature verification failed")
    if abs(clock() - timestamp) > tolerance_seconds:
        raise TimestampOutsideTolerance("signature timestamp outside tolerance")
    return timestamp
