"""Error base shared by all external-system clients."""

from __future__ import annotations


class IntegrationError(Exception):
    """A failure talking to an external system.

    ``retryable`` tells callers whether the same request may succeed later
    (timeouts, outages) or will keep failing until something changes
    (bad credentials, missing records, invalid payloads).
    """

    retryable: bool = False
    #: Stable machine-readable code recorded in sync history.
    code: str = "integration_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
