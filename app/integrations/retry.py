"""Bounded retry with capped exponential backoff and full jitter."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import TypeVar

from app.integrations.errors import IntegrationError

T = TypeVar("T")
logger = logging.getLogger(__name__)

#: Absolute ceiling regardless of configuration, so retries can never be unbounded.
MAX_RETRIES_CEILING = 5


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Full-jitter delay for the given 1-based attempt number."""
    return random.uniform(0, min(base * (2 ** (attempt - 1)), cap))


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_retries: int,
    backoff_base: float,
    backoff_max: float,
    sleep: Callable[[float], None],
    label: str,
) -> T:
    """Call ``fn``, retrying only ``IntegrationError``s marked retryable."""
    max_attempts = 1 + max(0, min(max_retries, MAX_RETRIES_CEILING))
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except IntegrationError as exc:
            if not exc.retryable or attempt == max_attempts:
                raise
            delay = backoff_delay(attempt, backoff_base, backoff_max)
            logger.warning(
                "integration.retry target=%s attempt=%d/%d error=%s delay=%.2fs",
                label, attempt, max_attempts, type(exc).__name__, delay,
            )
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
