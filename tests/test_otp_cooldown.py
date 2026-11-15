"""
The 60-second OTP resend cooldown is only kept when an OTP was actually sent.

User decision (2026-11-15): a request that fails before the message goes out —
duplicate email, number already registered, unknown number, a dead SMS gateway
— must not leave the caller waiting 60s for a code they never received. The
hourly caps are a separate matter and ARE still charged on failure, because
they are what keeps bulk number-probing expensive.

MongoDB is replaced by mongomock, so nothing here touches the network.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

TAKEN_MOBILE = "9876543210"
TAKEN_EMAIL = "taken@example.com"
ADMIN_MOBILE = "8484844053"
FRESH = "9000000111"


@pytest.fixture()
def cd(monkeypatch):
    from mongomock_motor import AsyncMongoMockClient

    from app.core import deps
    from app.core.config import settings
    from app.repositories import (
        favourite_repo, otp_repo, rate_limit_repo, refresh_token_repo,
        subscription_repo, user_repo, video_repo, watch_repo,
    )

    db = AsyncMongoMockClient()["cooldown_test"]
    for module, attr, name in [
        (favourite_repo, "favourites_collection", "favourites"),
        (watch_repo, "watch_history_collection", "watch_history"),
        (video_repo, "videos_collection", "videos"),
        (user_repo, "users_collection", "users"),
        (otp_repo, "otps_collection", "otps"),
        (refresh_token_repo, "refresh_tokens_collection", "refresh_tokens"),
        (subscription_repo, "subscriptions_collection", "subscriptions"),
        (rate_limit_repo, "rate_limits_collection", "rate_limits"),
    ]:
        monkeypatch.setattr(module, attr, db[name])

    # The whole point of this file: limits ON.
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "OTP_COOLDOWN_SECONDS", 60)
    monkeypatch.setattr(settings, "OTP_MAX_PER_MOBILE_PER_HOUR", 20)
    monkeypatch.setattr(settings, "OTP_MAX_PER_IP_PER_HOUR", 50)
    monkeypatch.setattr(settings, "ENFORCE_UNIQUE_EMAIL", True)
    monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
    monkeypatch.setattr(settings, "ADMIN_MOBILE_NUMBERS", ADMIN_MOBILE)
    deps.admin_mobile_numbers.cache_clear()

    now = datetime.now(timezone.utc)

    async def setup():
        await db["users"].insert_one({
            "_id": ObjectId(), "mobile_number": TAKEN_MOBILE, "name": "Taken",
            "email": TAKEN_EMAIL, "status": "active", "is_verified": True,
            "role": "user", "created_at": now,
        })
        await db["users"].insert_one({
            "_id": ObjectId(), "mobile_number": ADMIN_MOBILE, "name": "Admin",
            "status": "active", "is_verified": True, "role": "admin", "created_at": now,
        })
    asyncio.run(setup())

    from app.main import app

    class H:
        client = TestClient(app)

        def signup(self, mobile=FRESH, name="New User", **extra):
            return self.client.post("/api/v1/auth/signup",
                                    json={"mobile_number": mobile, "name": name, **extra})

        def signin(self, mobile):
            return self.client.post("/api/v1/auth/signin", json={"mobile_number": mobile})

        def delete_account(self, mobile):
            return self.client.post("/api/v1/auth/delete-account",
                                    json={"mobile_number": mobile})

        def counters(self, scope):
            return asyncio.run(db["rate_limits"].count_documents({"scope": scope}))

    return H()


def _code(response):
    return response.json().get("error", {}).get("code")


# ── Failures must not start the cooldown ─────────────────────────────────────

def test_duplicate_email_does_not_start_the_cooldown(cd):
    """The case the user hit: typo an email that belongs to someone else, fix
    it, retry — and the retry must go through at once."""
    first = cd.signup(email=TAKEN_EMAIL)
    assert first.status_code == 409 and _code(first) == "EMAIL_IN_USE", first.text

    second = cd.signup(email="mine@example.com")
    assert second.status_code == 201, second.text
    assert second.json()["data"]["dev_otp"]


def test_existing_account_does_not_start_the_cooldown(cd):
    first = cd.signup(mobile=TAKEN_MOBILE)
    assert first.status_code == 409 and _code(first) == "ACCOUNT_EXISTS", first.text

    # Same number, now via sign-in, which is the right endpoint for it.
    assert cd.signin(TAKEN_MOBILE).status_code == 200


def test_unknown_number_at_signin_does_not_start_the_cooldown(cd):
    first = cd.signin("9111111111")
    assert first.status_code == 404, first.text
    # Correct the typo and go again immediately.
    assert cd.signin(TAKEN_MOBILE).status_code == 200


def test_a_failed_sms_send_does_not_start_the_cooldown(cd, monkeypatch):
    """A dead gateway is the worst case to charge for: no OTP exists at all,
    so the user must be free to retry the moment it comes back."""
    from app.providers.sms import SMSDeliveryError
    from app.services import auth_service

    real = auth_service.send_otp_sms
    gateway = {"down": True}

    async def flaky(mobile_number, otp):
        if gateway["down"]:
            raise SMSDeliveryError("gateway down")
        return await real(mobile_number, otp)

    # Not monkeypatch.undo() to bring the gateway back: that would also undo the
    # fixture's collection patches and send the next call at the real database.
    monkeypatch.setattr(auth_service, "send_otp_sms", flaky)
    first = cd.signin(TAKEN_MOBILE)
    assert first.status_code >= 500 or _code(first) == "OTP_SEND_FAILED", first.text

    gateway["down"] = False
    assert cd.signin(TAKEN_MOBILE).status_code == 200


def test_refusing_to_delete_an_admin_does_not_start_the_cooldown(cd):
    first = cd.delete_account(ADMIN_MOBILE)
    assert first.status_code == 403 and _code(first) == "FORBIDDEN", first.text
    assert cd.signin(TAKEN_MOBILE).status_code == 200


# ── A real send still starts it ──────────────────────────────────────────────

def test_a_sent_otp_does_start_the_cooldown(cd):
    assert cd.signup().status_code == 201
    again = cd.signup()
    assert again.status_code == 429 and _code(again) == "RATE_LIMITED", again.text
    assert again.json()["error"]["details"]["retry_after_seconds"] >= 1


def test_a_rate_limited_retry_never_clears_the_live_cooldown(cd):
    """Hammering must not refund the cooldown that the first, successful send
    put there — otherwise the limit would be trivially bypassable."""
    assert cd.signup().status_code == 201
    for _ in range(3):
        assert cd.signup().status_code == 429
    assert cd.signin(FRESH).status_code == 429


def test_signin_cooldown_still_applies_after_a_successful_send(cd):
    assert cd.signin(TAKEN_MOBILE).status_code == 200
    assert cd.signin(TAKEN_MOBILE).status_code == 429


# ── What failure still costs ─────────────────────────────────────────────────

def test_the_hourly_caps_are_still_charged_on_failure(cd):
    """Number-probing must stay expensive: the per-hour counters are kept even
    though the cooldown is handed back."""
    for _ in range(4):
        cd.signup(mobile=TAKEN_MOBILE)
    assert cd.counters("otp_mobile_hour") == 1
    assert cd.counters("otp_ip_hour") == 1
    assert cd.counters("otp_cooldown") == 0, "the cooldown should have been handed back"


def test_the_hourly_cap_can_still_lock_a_number_out(cd, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "OTP_MAX_PER_MOBILE_PER_HOUR", 3)
    codes = [cd.signup(mobile=TAKEN_MOBILE).status_code for _ in range(5)]
    assert codes[:3] == [409, 409, 409], codes
    assert codes[3] == 429, codes
