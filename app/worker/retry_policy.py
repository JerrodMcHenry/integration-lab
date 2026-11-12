"""Retry classification and bounded exponential backoff with jitter.

Decision table for a processed event (``attempts`` counts claims, starting at 1):

| sync outcome                | attempts < max | attempts >= max |
|-----------------------------|----------------|-----------------|
| success                     | succeeded      | succeeded       |
| failure, retryable          | retry (pending, available_at = now + backoff) | dead_letter |
| failure, permanent          | failed (never retried automatically)           | failed      |

Backoff uses "equal jitter": d = min(max_delay, base * 2^(attempt-1)); the delay
is uniform in [d/2, d]. Unlike full jitter it guarantees a minimum spacing
between attempts, and it stays bounded by ``max_delay``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from app.core.config import RetrySettings

Decision = Literal["succeeded", "retry", "failed", "dead_letter"]


@dataclass(frozen=True)
class RetryPolicy:
    settings: RetrySettings
    max_attempts: int
    rng: random.Random = random.Random()

    def ceiling(self, attempt: int) -> float:
        return min(self.settings.max_delay_seconds, self.settings.base_delay_seconds * 2 ** max(0, attempt - 1))

    def delay(self, attempt: int) -> float:
        d = self.ceiling(attempt)
        return self.rng.uniform(d / 2, d)

    def decide(self, *, succeeded: bool, retryable: bool | None, attempt: int) -> Decision:
        if succeeded:
            return "succeeded"
        if not retryable:
            return "failed"
        return "retry" if attempt < self.max_attempts else "dead_letter"
