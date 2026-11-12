"""HTTP client for the Salesforce-style CRM.

Responsibilities:
- OAuth 2.0 client-credentials authentication with token caching.
- Translating transport/HTTP failures into a typed error hierarchy so callers
  can decide what is retryable and what is a permanent failure.
- Bounded retries with exponential backoff for transient failures on
  idempotent reads only.

Account payloads are never logged; only identifiers, status codes and timings.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import CRMSettings, get_settings
from app.integrations.errors import IntegrationError
from app.integrations.retry import call_with_retry

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class CRMError(IntegrationError):
    """Base class for all CRM integration errors."""

    code = "crm_error"


class CRMNotFoundError(CRMError):
    """The requested record does not exist in the CRM (permanent)."""

    code = "crm_not_found"


class CRMAuthError(CRMError):
    """Credentials were rejected (permanent until configuration is fixed)."""

    code = "crm_auth_failed"


class CRMRequestError(CRMError):
    """The CRM rejected the request as invalid (4xx, permanent)."""

    code = "crm_bad_request"


class CRMResponseError(CRMError):
    """The CRM returned a payload we could not interpret (permanent)."""

    code = "crm_invalid_response"


class CRMTimeoutError(CRMError):
    """The CRM did not respond within the configured timeout (transient)."""

    retryable = True
    code = "crm_timeout"


class CRMUnavailableError(CRMError):
    """Connection failure, 5xx, or 429 from the CRM (transient)."""

    retryable = True
    code = "crm_unavailable"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
_TOKEN_EXPIRY_SKEW_SECONDS = 30


class CRMClient:
    """Synchronous CRM client. Safe to share across threads."""

    def __init__(
        self,
        settings: CRMSettings | None = None,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings().crm
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=self._settings.timeout_seconds)
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> CRMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API -------------------------------------------------------- #
    def get_account(self, account_id: str) -> dict[str, Any]:
        """Fetch a single CRM account by its CRM Id."""
        if not account_id or not account_id.strip():
            raise CRMRequestError("account_id must be non-empty")
        path = f"/crm/accounts/{quote(account_id, safe='')}"
        payload = self._request_with_retry("GET", path, resource=f"account:{account_id}")
        if not isinstance(payload, dict) or "Id" not in payload:
            raise CRMResponseError("CRM account response missing required 'Id' field")
        return payload

    # -- auth -------------------------------------------------------------- #
    def _get_token(self, *, force_refresh: bool = False) -> str:
        with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._token and now < self._token_expires_at:
                return self._token

            try:
                response = self._http.post(
                    self._settings.token_url,
                    data={"grant_type": "client_credentials"},
                    auth=(self._settings.client_id, self._settings.client_secret),
                    timeout=self._settings.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                raise CRMTimeoutError("Timed out requesting CRM access token") from exc
            except httpx.TransportError as exc:
                raise CRMUnavailableError(f"CRM token endpoint unreachable: {type(exc).__name__}") from exc

            if response.status_code in (400, 401, 403):
                raise CRMAuthError("CRM rejected client credentials", status_code=response.status_code)
            if response.status_code >= 500 or response.status_code == 429:
                raise CRMUnavailableError("CRM token endpoint unavailable", status_code=response.status_code)
            if response.status_code != 200:
                raise CRMResponseError("Unexpected token endpoint status", status_code=response.status_code)

            try:
                body = response.json()
                token = body["access_token"]
                expires_in = int(body.get("expires_in", 300))
            except (ValueError, KeyError, TypeError) as exc:
                raise CRMResponseError("Malformed CRM token response") from exc

            self._token = token
            self._token_expires_at = now + max(expires_in - _TOKEN_EXPIRY_SKEW_SECONDS, 0)
            logger.info("crm.token.acquired expires_in=%s", expires_in)
            return token

    # -- request plumbing -------------------------------------------------- #
    def _request_with_retry(self, method: str, path: str, *, resource: str) -> Any:
        # Only idempotent methods are retried automatically.
        retries = self._settings.max_retries if method in ("GET", "HEAD") else 0
        return call_with_retry(
            lambda: self._request_once(method, path, resource=resource),
            max_retries=retries,
            backoff_base=self._settings.backoff_base_seconds,
            backoff_max=self._settings.backoff_max_seconds,
            sleep=self._sleep,
            label=f"crm:{resource}",
        )

    def _request_once(self, method: str, path: str, *, resource: str) -> Any:
        url = f"{self._settings.base_url}{path}"
        started = time.monotonic()

        response = self._send(method, url, self._get_token())
        if response.status_code == 401:
            # Token may have been revoked or expired early; refresh once.
            response = self._send(method, url, self._get_token(force_refresh=True))

        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "crm.request method=%s resource=%s status=%d elapsed_ms=%.0f",
            method, resource, response.status_code, elapsed_ms,
        )

        status = response.status_code
        if status == 404:
            raise CRMNotFoundError(f"CRM {resource} not found", status_code=status)
        if status in (401, 403):
            raise CRMAuthError(f"CRM denied access to {resource}", status_code=status)
        if status == 429 or status >= 500:
            raise CRMUnavailableError(f"CRM unavailable for {resource}", status_code=status)
        if 400 <= status < 500:
            raise CRMRequestError(f"CRM rejected request for {resource}", status_code=status)

        try:
            return response.json()
        except ValueError as exc:
            raise CRMResponseError(f"CRM returned non-JSON body for {resource}") from exc

    def _send(self, method: str, url: str, token: str) -> httpx.Response:
        try:
            return self._http.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=self._settings.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise CRMTimeoutError(f"CRM request timed out after {self._settings.timeout_seconds}s") from exc
        except httpx.TransportError as exc:
            raise CRMUnavailableError(f"CRM unreachable: {type(exc).__name__}") from exc


def fetch_crm_account(account_id: str) -> dict[str, Any]:
    """Backward-compatible helper wrapping :class:`CRMClient`."""
    with CRMClient() as client:
        return client.get_account(account_id)
