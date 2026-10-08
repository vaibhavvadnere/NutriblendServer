"""
End-to-end UI test of the login flow with Streamlit's AppTest, against a fake
server (httpx.MockTransport). Proves: login gate, OTP step, sign-in, the
dashboard page rendering, and logout.
"""

from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from dashboard.auth import session

MAIN = str(Path(__file__).resolve().parents[1] / "main.py")

USER = {
    "id": "u1", "mobile_number": "9876543210", "name": "Vaibhav", "status": "active",
    "is_verified": True, "created_at": "2026-09-01T10:00:00Z",
}


@pytest.fixture(autouse=True)
def _no_remember(monkeypatch):
    """These tests are about the form itself; the 'stay signed in' wait has its own tests."""
    from dashboard.config import settings
    monkeypatch.setattr(settings, "SESSION_REMEMBER_HOURS", 0)


def _ok(data):
    return httpx.Response(200, json={"success": True, "message": "ok", "data": data})


def fake_server(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path == "/api/v1/admin/auth/signin":
        import json
        if json.loads(req.content)["mobile_number"] == "9000000009":
            return httpx.Response(429, headers={"Retry-After": "40"}, json={"success": False,
                "message": "Please wait before requesting another OTP.", "error": {"code": "RATE_LIMITED"}})
        if json.loads(req.content)["mobile_number"] != "9876543210":
            return httpx.Response(403, json={"success": False, "message": "This mobile number is not registered as an admin.",
                                             "error": {"code": "FORBIDDEN"}})
        return _ok({"mobile_number": "98XXXXXX10", "expires_in_minutes": 5,
                    "resend_available_in_seconds": 60, "is_new_account": False, "dev_otp": "123456"})
    if path == "/api/v1/auth/verify-otp":
        import json
        if json.loads(req.content)["otp"] != "123456":
            return httpx.Response(400, json={"success": False, "message": "That OTP is incorrect.",
                                             "error": {"code": "OTP_INCORRECT"}})
        access = "a" * 30 if json.loads(req.content)["mobile_number"] == "9876543210" else "n" * 30
        return _ok({"access_token": access, "refresh_token": "r" * 30, "expires_in": 1800, "user": USER})
    if path == "/api/v1/admin/me":
        if req.headers.get("Authorization") != "Bearer " + "a" * 30:
            return httpx.Response(403, json={"success": False, "message": "This account does not have admin access.",
                                             "error": {"code": "FORBIDDEN"}})
        return _ok({"user": USER, "is_admin": True})
    if path == "/api/v1/admin/stats":
        return _ok({"total_users": 3, "users_by_status": {"active": 2, "pending": 1, "blocked": 0},
                    "signups_today": 1, "signups_last_7_days": 3, "active_last_7_days": 2,
                    "signups_by_day": [{"date": "2026-09-27", "count": 1}], "timezone": "Asia/Kolkata"})
    if path == "/api/health":
        return _ok({"status": "ok", "app": "Nutriblend API", "env": "development",
                    "database": "ok", "sms_provider": "mock"})
    if path == "/api/v1/auth/logout":
        return _ok({})
    return httpx.Response(404, json={"success": False, "message": "no", "error": {"code": "NOT_FOUND"}})


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(fake_server))
    monkeypatch.setattr(session, "_shared_http", lambda: client)


def test_full_login_flow():
    at = AppTest.from_file(MAIN, default_timeout=10).run()
    assert not at.exception
    assert at.title[0].value == "Nutriblend Admin"

    # Step 1: mobile number
    at.text_input[0].input("98765 43210")
    at.button[0].click().run()
    assert not at.exception
    assert any("98XXXXXX10" in i.value for i in at.info)

    # Wrong OTP shows the server's message
    at.text_input[0].input("000000")
    at.button[0].click().run()
    assert any("incorrect" in e.value for e in at.error)

    # Right OTP signs in and shows the dashboard
    at.text_input[0].input("123456")
    at.button[0].click().run()
    assert not at.exception
    assert at.title[0].value == "Dashboard"
    assert any(m.label == "Total users" and m.value == "3" for m in at.metric)

    # Log out -> back to sign in
    next(b for b in at.button if b.label == "Log out").click().run()
    assert at.title[0].value == "Nutriblend Admin"
    assert "auth.refresh_token" not in at.session_state


def test_invalid_mobile_is_rejected_locally():
    at = AppTest.from_file(MAIN, default_timeout=10).run()
    at.text_input[0].input("12345")
    at.button[0].click().run()
    assert any("10-digit" in e.value for e in at.error)


def test_non_admin_is_refused_before_otp():
    at = AppTest.from_file(MAIN, default_timeout=10).run()
    at.text_input[0].input("9123456780")
    at.button[0].click().run()
    assert not at.exception
    assert any("not registered as an admin" in e.value for e in at.error)
    assert "auth.refresh_token" not in at.session_state
    assert at.title[0].value == "Nutriblend Admin"


def test_cooldown_lets_you_enter_existing_otp():
    at = AppTest.from_file(MAIN, default_timeout=10).run()
    at.text_input[0].input("9000000009")
    at.button[0].click().run()
    assert not at.exception
    assert any("already sent" in w.value and "40s" in w.value for w in at.warning)
    next(b for b in at.button if b.label == "Enter the OTP I already have").click().run()
    assert any("90XXXXXX09" in i.value for i in at.info)   # now on the OTP step
    assert any(t.label == "OTP" for t in at.text_input)
