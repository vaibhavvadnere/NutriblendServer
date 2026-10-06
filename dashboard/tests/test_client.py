"""
Tests for api/client.py against a fake server (httpx.MockTransport) —
no real server or database needed.

    .venv/bin/python -m pytest
"""

import time

import httpx
import pytest

from dashboard.api import auth as auth_api
from dashboard.api.client import ApiClient, MemoryTokenStore
from dashboard.api.errors import (
    Forbidden,
    NetworkError,
    RateLimited,
    SessionExpired,
    ValidationFailed,
)

USER = {
    "id": "u1", "mobile_number": "9876543210", "name": "Admin", "status": "active",
    "is_verified": True, "created_at": "2026-09-01T10:00:00Z",
}


def ok(data, status=200):
    return httpx.Response(status, json={"success": True, "message": "ok", "data": data})


def fail(status, code, message="nope", details=None, headers=None):
    error = {"code": code, **({"details": details} if details else {})}
    return httpx.Response(status, json={"success": False, "message": message, "error": error}, headers=headers)


def make_client(handler, tokens=None):
    http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    return ApiClient(tokens=tokens or MemoryTokenStore(), http=http, base_url="http://test")


def logged_in(expires_in=1800):
    store = MemoryTokenStore()
    store.set_tokens("access-1", "refresh-1" * 3, expires_in)
    return store


def test_unwraps_data():
    client = make_client(lambda req: ok({"x": 1}))
    assert client.get("/api/v1/thing") == {"x": 1}


def test_sends_bearer_token():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("Authorization")
        return ok({})

    make_client(handler, logged_in()).get("/api/v1/thing")
    assert seen["auth"] == "Bearer access-1"


def test_public_calls_send_no_token():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("Authorization")
        return ok({})

    make_client(handler, logged_in()).post("/api/v1/auth/signin", auth=False, json={})
    assert seen["auth"] is None


@pytest.mark.parametrize(
    "status,code,exc",
    [(403, "ACCOUNT_BLOCKED", Forbidden), (422, "VALIDATION_ERROR", ValidationFailed)],
)
def test_error_mapping(status, code, exc):
    client = make_client(lambda req: fail(status, code, details={"fields": {"otp": "bad"}}))
    with pytest.raises(exc) as info:
        client.get("/x", auth=False)
    assert info.value.code == code and info.value.status_code == status


def test_rate_limited_reads_retry_after():
    client = make_client(lambda req: fail(429, "RATE_LIMITED", headers={"Retry-After": "42"}))
    with pytest.raises(RateLimited) as info:
        client.get("/x", auth=False)
    assert info.value.retry_after == 42


def test_non_envelope_response():
    client = make_client(lambda req: httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(Exception) as info:
        client.get("/x", auth=False)
    assert info.value.code == "BAD_RESPONSE"


def test_network_error():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(NetworkError):
        make_client(handler).get("/x", auth=False)


def test_401_refreshes_and_retries():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/api/v1/auth/refresh":
            return ok({"access_token": "access-2", "refresh_token": "refresh-2" * 3, "expires_in": 1800})
        if req.headers["Authorization"] == "Bearer access-1":
            return fail(401, "INVALID_TOKEN")
        return ok({"fine": True})

    tokens = logged_in()
    assert make_client(handler, tokens).get("/api/v1/users/me") == {"fine": True}
    assert calls == ["/api/v1/users/me", "/api/v1/auth/refresh", "/api/v1/users/me"]
    assert tokens.get_access() == "access-2"


def test_failed_refresh_expires_session():
    def handler(req):
        if req.url.path == "/api/v1/auth/refresh":
            return fail(401, "REFRESH_TOKEN_INVALID")
        return fail(401, "INVALID_TOKEN")

    tokens = logged_in()
    with pytest.raises(SessionExpired):
        make_client(handler, tokens).get("/api/v1/users/me")
    assert tokens.get_access() is None and tokens.get_refresh() is None


def test_refreshes_before_expiry():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/api/v1/auth/refresh":
            return ok({"access_token": "access-2", "refresh_token": "refresh-2" * 3, "expires_in": 1800})
        return ok({"auth": req.headers["Authorization"]})

    tokens = logged_in(expires_in=5)  # inside the refresh leeway
    assert make_client(handler, tokens).get("/api/v1/users/me") == {"auth": "Bearer access-2"}
    assert calls[0] == "/api/v1/auth/refresh"
    assert tokens.get_expires_at() > time.time() + 1000


def test_verify_otp_stores_tokens():
    def handler(req):
        return ok({"access_token": "a" * 20, "refresh_token": "r" * 20, "expires_in": 1800, "user": USER})

    tokens = MemoryTokenStore()
    result = auth_api.verify_otp(make_client(handler, tokens), "9876543210", "123456")
    assert result.user.name == "Admin"
    assert tokens.get_access() == "a" * 20


def test_logout_clears_tokens_even_if_server_down():
    def handler(req):
        raise httpx.ConnectError("down")

    tokens = logged_in()
    auth_api.logout(make_client(handler, tokens))
    assert tokens.get_refresh() is None
