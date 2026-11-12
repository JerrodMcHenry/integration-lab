"""Unit tests for CRMClient using httpx.MockTransport (no network)."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import CRMSettings
from app.integrations.crm_client import (
    CRMAuthError,
    CRMClient,
    CRMNotFoundError,
    CRMRequestError,
    CRMResponseError,
    CRMTimeoutError,
    CRMUnavailableError,
)

ACCOUNT = {"Id": "SF-1001", "Name": "Acme Capital", "OwnerName": "Sarah Johnson"}


def make_settings(**overrides) -> CRMSettings:
    values = dict(
        base_url="http://crm.test",
        token_url="http://crm.test/oauth/token",
        client_id="cid",
        client_secret="secret",
        timeout_seconds=1.0,
        max_retries=2,
        backoff_base_seconds=0.1,
        backoff_max_seconds=1.0,
    )
    values.update(overrides)
    return CRMSettings(**values)


class FakeCRM:
    """Scriptable transport handler that records every request."""

    def __init__(self, account_responses):
        self.account_responses = list(account_responses)
        self.token_status = 200
        self.token_calls = 0
        self.account_calls = 0
        self.auth_headers: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(
                200, json={"access_token": f"tok-{self.token_calls}", "expires_in": 3600}
            )
        self.account_calls += 1
        self.auth_headers.append(request.headers.get("Authorization", ""))
        result = self.account_responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_client(fake: FakeCRM, **settings_overrides):
    sleeps: list[float] = []
    client = CRMClient(
        make_settings(**settings_overrides),
        http_client=httpx.Client(transport=httpx.MockTransport(fake)),
        sleep=sleeps.append,
    )
    return client, sleeps


def test_get_account_success_uses_bearer_token():
    fake = FakeCRM([httpx.Response(200, json=ACCOUNT)])
    client, _ = make_client(fake)

    assert client.get_account("SF-1001") == ACCOUNT
    assert fake.auth_headers == ["Bearer tok-1"]


def test_token_is_cached_across_requests():
    fake = FakeCRM([httpx.Response(200, json=ACCOUNT), httpx.Response(200, json=ACCOUNT)])
    client, _ = make_client(fake)

    client.get_account("SF-1001")
    client.get_account("SF-1001")

    assert fake.token_calls == 1


def test_401_triggers_single_token_refresh():
    fake = FakeCRM([httpx.Response(401), httpx.Response(200, json=ACCOUNT)])
    client, _ = make_client(fake)

    assert client.get_account("SF-1001") == ACCOUNT
    assert fake.token_calls == 2
    assert fake.auth_headers == ["Bearer tok-1", "Bearer tok-2"]


def test_persistent_401_raises_auth_error_without_retry():
    fake = FakeCRM([httpx.Response(401), httpx.Response(401)])
    client, sleeps = make_client(fake)

    with pytest.raises(CRMAuthError):
        client.get_account("SF-1001")
    assert sleeps == []


def test_invalid_client_credentials_raise_auth_error():
    fake = FakeCRM([])
    fake.token_status = 401
    client, _ = make_client(fake)

    with pytest.raises(CRMAuthError):
        client.get_account("SF-1001")
    assert fake.account_calls == 0


def test_not_found_is_permanent():
    fake = FakeCRM([httpx.Response(404, json={"detail": "Account not found"})])
    client, sleeps = make_client(fake)

    with pytest.raises(CRMNotFoundError) as exc_info:
        client.get_account("SF-9999")
    assert exc_info.value.status_code == 404
    assert exc_info.value.retryable is False
    assert sleeps == []


def test_bad_request_is_permanent():
    fake = FakeCRM([httpx.Response(400)])
    client, _ = make_client(fake)

    with pytest.raises(CRMRequestError):
        client.get_account("SF-1001")


def test_transient_5xx_is_retried_then_succeeds():
    fake = FakeCRM([httpx.Response(503), httpx.Response(200, json=ACCOUNT)])
    client, sleeps = make_client(fake)

    assert client.get_account("SF-1001") == ACCOUNT
    assert fake.account_calls == 2
    assert len(sleeps) == 1


def test_retries_are_bounded():
    fake = FakeCRM([httpx.Response(503)] * 10)
    client, sleeps = make_client(fake, max_retries=2)

    with pytest.raises(CRMUnavailableError) as exc_info:
        client.get_account("SF-1001")
    assert exc_info.value.retryable is True
    assert fake.account_calls == 3  # 1 attempt + 2 retries
    assert len(sleeps) == 2


def test_backoff_delays_respect_cap():
    fake = FakeCRM([httpx.Response(503)] * 6)
    client, sleeps = make_client(
        fake, max_retries=5, backoff_base_seconds=1.0, backoff_max_seconds=2.0
    )

    with pytest.raises(CRMUnavailableError):
        client.get_account("SF-1001")
    assert len(sleeps) == 5
    assert all(0 <= s <= 2.0 for s in sleeps)


def test_timeout_maps_to_timeout_error():
    fake = FakeCRM([httpx.ReadTimeout("slow")] * 3)
    client, sleeps = make_client(fake)

    with pytest.raises(CRMTimeoutError):
        client.get_account("SF-1001")
    assert fake.account_calls == 3
    assert len(sleeps) == 2


def test_connection_error_maps_to_unavailable():
    fake = FakeCRM([httpx.ConnectError("refused")])
    client, _ = make_client(fake, max_retries=0)

    with pytest.raises(CRMUnavailableError):
        client.get_account("SF-1001")


def test_non_json_body_raises_response_error():
    fake = FakeCRM([httpx.Response(200, text="<html>oops</html>")])
    client, _ = make_client(fake)

    with pytest.raises(CRMResponseError):
        client.get_account("SF-1001")


def test_payload_missing_id_raises_response_error():
    fake = FakeCRM([httpx.Response(200, json={"Name": "No Id"})])
    client, _ = make_client(fake)

    with pytest.raises(CRMResponseError):
        client.get_account("SF-1001")


def test_account_id_is_url_encoded():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        seen["raw_path"] = request.url.raw_path
        return httpx.Response(200, json={"Id": "x"})

    client = CRMClient(make_settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.get_account("../admin")
    assert seen["raw_path"] == b"/crm/accounts/..%2Fadmin"


def test_empty_account_id_rejected_without_request():
    fake = FakeCRM([])
    client, _ = make_client(fake)

    with pytest.raises(CRMRequestError):
        client.get_account("  ")
    assert fake.token_calls == 0


def test_account_payload_not_logged(caplog):
    secret_value = "Very Confidential Holdings"
    fake = FakeCRM([httpx.Response(200, json={"Id": "SF-1", "Name": secret_value})])
    client, _ = make_client(fake)

    with caplog.at_level("DEBUG", logger="app.integrations.crm_client"):
        client.get_account("SF-1")
    assert secret_value not in caplog.text
    assert "tok-1" not in caplog.text


@pytest.mark.parametrize("bad", [{"max_retries": 6}, {"max_retries": -1}, {"timeout_seconds": 0}])
def test_settings_validation(bad):
    with pytest.raises(ValueError):
        make_settings(**bad)
