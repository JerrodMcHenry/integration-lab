"""Worker and webhook configuration bounds (no database)."""

from __future__ import annotations

import dataclasses

import pytest

from app.core.config import WebhookSettings, WorkerSettings, load_settings
from app.worker.runner import WorkerConfigurationError, resolve_lease_seconds


def with_worker(**kw):
    s = load_settings()
    return dataclasses.replace(s, worker=WorkerSettings(**kw))


def test_default_lease_covers_worst_case_with_margin():
    s = load_settings()
    from app.core.config import worst_case_sync_seconds
    needed = worst_case_sync_seconds(s.crm, s.internal) + s.database.lock_timeout_seconds
    assert resolve_lease_seconds(s) >= 2 * needed


def test_lease_shorter_than_worst_case_is_refused():
    with pytest.raises(WorkerConfigurationError, match="shorter than the worst-case"):
        resolve_lease_seconds(with_worker(lease_seconds=5))


@pytest.mark.parametrize("kw", [{"batch_size": 0}, {"batch_size": 101}, {"max_attempts": 0},
                                {"max_attempts": 21}, {"poll_interval_seconds": 0}, {"lease_seconds": 0.5}])
def test_worker_settings_bounds(kw):
    with pytest.raises(ValueError):
        WorkerSettings(**kw)


@pytest.mark.parametrize("kw", [{"tolerance_seconds": 5}, {"tolerance_seconds": 7200},
                                {"max_body_bytes": 10}, {"max_body_bytes": 10 * 1024 * 1024}])
def test_webhook_settings_bounds(kw):
    with pytest.raises(ValueError):
        WebhookSettings(**kw)
