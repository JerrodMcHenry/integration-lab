"""HTTP client for Meridian's internal customer management API.

Kept separate from the CRM client and from sync logic. It knows how to talk to
the destination system and nothing about field mapping or ownership.

Retry policy:
- GET and PATCH are idempotent and retried on transient failures.
- POST (create) is retried only because every create carries an
  ``Idempotency-Key``: the destination returns the original result for a
  repeated key instead of creating a duplicate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import InternalSystemSettings, get_settings
from app.integrations.errors import IntegrationError
from app.integrations.retry import call_with_retry

logger = logging.getLogger(__name__)


class InternalSystemError(IntegrationError):
    code = "destination_error"


class InternalAuthError(InternalSystemError):
    code = "destination_auth_failed"


class InternalNotFoundError(InternalSystemError):
    code = "destination_not_found"


class InternalConflictError(InternalSystemError):
    """A customer with this external CRM id already exists."""

    code = "destination_conflict"

    def __init__(self, message: str, *, status_code: int | None = None,
                 existing_customer_id: str | None = None) -> None:
        super().__init__(message, status_code=status_code)
        self.existing_customer_id = existing_customer_id


class InternalValidationError(InternalSystemError):
    code = "destination_rejected_payload"


class InternalResponseError(InternalSystemError):
    code = "destination_invalid_response"


class InternalTimeoutError(InternalSystemError):
    retryable = True
    code = "destination_timeout"


class InternalUnavailableError(InternalSystemError):
    retryable = True
    code = "destination_unavailable"


class InternalSystemClient:
    def __init__(
        self,
        settings: InternalSystemSettings | None = None,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings().internal
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=self._settings.timeout_seconds)
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> InternalSystemClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- public API -------------------------------------------------------- #
    def get_customer_by_external_id(self, external_crm_id: str) -> dict[str, Any] | None:
        """Return the customer linked to a CRM account, or ``None`` if not yet created."""
        path = f"/api/v1/customers/by-external-id/{quote(external_crm_id, safe='')}"
        try:
            return self._call("GET", path, resource=f"customer:ext:{external_crm_id}")
        except InternalNotFoundError:
            return None

    def create_customer(self, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return self._call(
            "POST", "/api/v1/customers", json=payload,
            headers={"Idempotency-Key": idempotency_key},
            resource=f"customer:ext:{payload.get('external_crm_id')}",
        )

    def update_customer(self, customer_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        path = f"/api/v1/customers/{quote(customer_id, safe='')}"
        return self._call("PATCH", path, json=changes, resource=f"customer:{customer_id}")

    # -- plumbing ---------------------------------------------------------- #
    def _call(self, method: str, path: str, *, resource: str,
              json: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None) -> Any:
        return call_with_retry(
            lambda: self._request_once(method, path, resource=resource, json=json, headers=headers),
            max_retries=self._settings.max_retries,
            backoff_base=self._settings.backoff_base_seconds,
            backoff_max=self._settings.backoff_max_seconds,
            sleep=self._sleep,
            label=f"internal:{resource}",
        )

    def _request_once(self, method: str, path: str, *, resource: str,
                      json: dict[str, Any] | None, headers: dict[str, str] | None) -> Any:
        url = f"{self._settings.base_url}{path}"
        all_headers = {"Authorization": f"Bearer {self._settings.api_key}", "Accept": "application/json"}
        all_headers.update(headers or {})
        started = time.monotonic()
        try:
            response = self._http.request(method, url, json=json, headers=all_headers,
                                          timeout=self._settings.timeout_seconds)
        except httpx.TimeoutException as exc:
            raise InternalTimeoutError(
                f"Internal API timed out after {self._settings.timeout_seconds}s") from exc
        except httpx.TransportError as exc:
            raise InternalUnavailableError(f"Internal API unreachable: {type(exc).__name__}") from exc

        status = response.status_code
        logger.info("internal.request method=%s resource=%s status=%d elapsed_ms=%.0f",
                    method, resource, status, (time.monotonic() - started) * 1000)

        if status == 404:
            raise InternalNotFoundError(f"Internal {resource} not found", status_code=status)
        if status in (401, 403):
            raise InternalAuthError(f"Internal API denied access to {resource}", status_code=status)
        if status == 409:
            existing = None
            try:
                existing = response.json().get("detail", {}).get("customer_id")
            except (ValueError, AttributeError):
                pass
            raise InternalConflictError(f"Internal {resource} already exists",
                                        status_code=status, existing_customer_id=existing)
        if status in (400, 422):
            raise InternalValidationError(f"Internal API rejected payload for {resource}", status_code=status)
        if status == 429 or status >= 500:
            raise InternalUnavailableError(f"Internal API unavailable for {resource}", status_code=status)
        if not 200 <= status < 300:
            raise InternalResponseError(f"Unexpected status from internal API for {resource}",
                                        status_code=status)
        try:
            body = response.json()
        except ValueError as exc:
            raise InternalResponseError(f"Internal API returned non-JSON body for {resource}") from exc
        if not isinstance(body, dict) or "customer_id" not in body:
            raise InternalResponseError(f"Internal API response for {resource} missing customer_id")
        return body
