"""
Passwords: optional at signup (bcrypt only), and email+password sign-in.

MongoDB is replaced by mongomock (with the same unique index production uses),
so nothing here touches the network.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

USER_MOBILE = "9876543210"
OTHER_MOBILE = "9000000001"
ADMIN_MOBILE = "8484844053"


@pytest.fixture()
def pw(monkeypatch):
    from mongomock_motor import AsyncMongoMockClient

    from app.core import deps
    from app.core.config import settings
    from app.repositories import (
        favourite_repo, otp_repo, rate_limit_repo, refresh_token_repo, subscription_repo, user_repo, video_repo,
        watch_repo,
    )

    db = AsyncMongoMockClient()["password_test"]
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
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
    monkeypatch.setattr(settings, "PAYMENTS_LOCK_VIDEOS", False)
    monkeypatch.setattr(settings, "ADMIN_MOBILE_NUMBERS", "")
    monkeypatch.setattr(settings, "WATCH_DEDUPE_SECONDS", 300)
    monkeypatch.setattr(settings, "WATCH_MAX_PLAYS_PER_HOUR", 200)
    deps.admin_mobile_numbers.cache_clear()

    now = datetime.now(timezone.utc)

    async def setup():
        await db["favourites"].create_index([("user_id", 1), ("video_id", 1)], unique=True)
        await db["watch_history"].create_index([("user_id", 1), ("video_id", 1)], unique=True)
        await db["subscriptions"].create_index("user_id", unique=True)
        for mobile, role in ((USER_MOBILE, "user"), (OTHER_MOBILE, "user"), (ADMIN_MOBILE, "admin")):
            await db["users"].insert_one({"_id": ObjectId(), "mobile_number": mobile, "name": mobile, "status": "active",
                                          "is_verified": True, "role": role, "created_at": now})
    asyncio.run(setup())

    from app.core.security import SCOPE_ADMIN, create_access_token
    from app.main import app

    class H:
        client = TestClient(app)
        user = {"Authorization": "Bearer " + create_access_token(USER_MOBILE)}
        other = {"Authorization": "Bearer " + create_access_token(OTHER_MOBILE)}
        admin = {"Authorization": "Bearer " + create_access_token(ADMIN_MOBILE, scope=SCOPE_ADMIN)}
        settings_ = settings
        database = db

        def video(self, title="Video", status="published", document=None):
            doc = {"_id": ObjectId(), "title": title, "status": status, "sort_order": 0,
                   "file": {"key": f"videos/{title}.mp4", "size": 1000, "content_type": "video/mp4"},
                   "created_at": now, "updated_at": now, "published_at": now}
            if document:
                doc["document"] = document
            asyncio.run(db["videos"].insert_one(doc))
            return str(doc["_id"])

        def signup(self, mobile="9000000111", name="New User", **extra):
            body = {"mobile_number": mobile, "name": name, **extra}
            return self.client.post("/api/v1/auth/signup", json=body)

        def verify(self, mobile, otp):
            return self.client.post("/api/v1/auth/verify-otp",
                                    json={"mobile_number": mobile, "otp": otp})

        def login(self, email, password, headers=None):
            return self.client.post("/api/v1/auth/signin",
                                    json={"email": email, "password": password})

        def otp_signin(self, mobile):
            return self.client.post("/api/v1/auth/signin", json={"mobile_number": mobile})

        def active_user(self, mobile="9000000111", email="boss@example.com", password="hunter2hunter2"):
            """Sign up WITH a password and verify the OTP, so the account is active."""
            r = self.signup(mobile=mobile, email=email, password=password)
            otp = r.json()["data"]["dev_otp"]
            self.verify(mobile, otp)
            return mobile, email, password

        def row(self, mobile):
            return asyncio.run(db["users"].find_one({"mobile_number": mobile}))

    return H()




# ── Optional at signup ───────────────────────────────────────────────────────

def test_signup_without_a_password_stores_no_field(pw):
    r = pw.signup()
    assert r.status_code == 201, r.text
    row = pw.row("9000000111")
    assert "password_hash" not in row, "the field should be absent, not null"


def test_signup_with_a_password_stores_a_bcrypt_hash(pw):
    r = pw.signup(password="hunter2hunter2")
    assert r.status_code == 201, r.text

    row = pw.row("9000000111")
    stored = row["password_hash"]
    assert stored.startswith("$2b$"), stored[:10]
    assert "hunter2hunter2" not in stored, "the password must never appear in the record"

    from app.core.security import verify_password
    assert verify_password("hunter2hunter2", stored) is True
    assert verify_password("wrong", stored) is False


def test_two_accounts_with_the_same_password_get_different_hashes(pw):
    """Salted — otherwise one cracked hash would expose every reuse."""
    pw.signup(mobile="9000000111", password="hunter2hunter2")
    pw.signup(mobile="9000000222", password="hunter2hunter2")
    assert pw.row("9000000111")["password_hash"] != pw.row("9000000222")["password_hash"]


# ── Validation ───────────────────────────────────────────────────────────────

def test_password_length_is_enforced(pw):
    for bad in ("short", "x" * 80):
        r = pw.signup(password=bad)
        assert r.status_code == 422, (bad[:10], r.text)
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert pw.row("9000000111") is None, "a rejected signup creates nothing"


def test_blank_password_is_treated_as_not_given(pw):
    r = pw.signup(password="   ")
    assert r.status_code == 201, r.text
    assert "password_hash" not in pw.row("9000000111")


# ── It must never leak ───────────────────────────────────────────────────────

def test_the_hash_is_never_returned_anywhere(pw):
    r = pw.signup(password="hunter2hunter2")
    assert "password" not in r.text and "$2b$" not in r.text

    otp = r.json()["data"]["dev_otp"]
    r = pw.verify("9000000111", otp)
    assert r.status_code == 200, r.text
    assert "$2b$" not in r.text and "hunter2hunter2" not in r.text

    token = r.json()["data"]["access_token"]
    me = pw.client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert "$2b$" not in me.text and "password_hash" not in me.text


def test_has_password_flag_tells_the_app_which_accounts_have_one(pw):
    for mobile, password in (("9000000111", "hunter2hunter2"), ("9000000222", None)):
        r = pw.signup(mobile=mobile, **({"password": password} if password else {}))
        otp = r.json()["data"]["dev_otp"]
        data = pw.verify(mobile, otp).json()["data"]["user"]
        assert data["has_password"] is bool(password), (mobile, data)


# ── Retrying a signup ────────────────────────────────────────────────────────

def test_retrying_signup_without_a_password_keeps_the_earlier_one(pw):
    """Same rule as email: omitting an optional field must not erase it."""
    pw.signup(password="hunter2hunter2")
    first = pw.row("9000000111")["password_hash"]
    pw.signup(name="Renamed")
    row = pw.row("9000000111")
    assert row["name"] == "Renamed", "the name is updated"
    assert row["password_hash"] == first, "but the password survives"


def test_retrying_signup_with_a_new_password_replaces_it(pw):
    pw.signup(password="hunter2hunter2")
    first = pw.row("9000000111")["password_hash"]
    pw.signup(password="brandnewpassword")
    from app.core.security import verify_password
    stored = pw.row("9000000111")["password_hash"]
    assert stored != first
    assert verify_password("brandnewpassword", stored) is True


# ── Deletion ─────────────────────────────────────────────────────────────────

def test_deleting_the_account_removes_the_password(pw):
    """A deleted account must not keep a usable credential."""
    from app.repositories import user_repo
    from datetime import datetime, timezone as tz
    pw.signup(password="hunter2hunter2")
    asyncio.run(user_repo.soft_delete("9000000111", datetime.now(tz.utc)))
    row = asyncio.run(pw.database["users"].find_one({"status": "deleted"}))
    assert "password_hash" not in row, row


# ── Email + password sign-in ─────────────────────────────────────────────────

def test_password_signin_returns_a_session_with_no_otp(pw):
    _, email, password = pw.active_user()
    r = pw.login(email, password)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["access_token"] and data["refresh_token"] and data["scope"] == "app"
    assert data["user"]["has_password"] is True
    assert "dev_otp" not in data, "no OTP is involved on this path"

    me = pw.client.get("/api/v1/users/me",
                       headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200, me.text


def test_the_mobile_path_is_unchanged(pw):
    mobile, _, _ = pw.active_user()
    r = pw.otp_signin(mobile)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "access_token" not in data, "mobile sign-in must still send an OTP"
    assert data["dev_otp"] and data["expires_in_minutes"] and data["sent_to"]


def test_wrong_password_and_unknown_email_look_identical(pw):
    """Otherwise the endpoint tells an attacker which addresses have accounts."""
    _, email, _ = pw.active_user()
    wrong = pw.login(email, "completely-wrong-x")
    unknown = pw.login("nobody@example.com", "completely-wrong-x")
    for r in (wrong, unknown):
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert wrong.json()["message"] == unknown.json()["message"]


def test_an_account_without_a_password_cannot_sign_in_with_one(pw):
    r = pw.signup(mobile="9000000222", email="nopass@example.com")
    pw.verify("9000000222", r.json()["data"]["dev_otp"])
    r = pw.login("nopass@example.com", "hunter2hunter2")
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS", r.text


def test_a_pending_account_cannot_sign_in_with_its_password(pw):
    """The crucial rule: a password must never bypass proving the mobile number."""
    pw.signup(mobile="9000000333", email="pending@example.com", password="hunter2hunter2")
    assert pw.row("9000000333")["status"] == "pending"
    r = pw.login("pending@example.com", "hunter2hunter2")
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS", r.text


def test_a_blocked_account_cannot_sign_in_with_its_password(pw):
    _, email, password = pw.active_user()
    asyncio.run(pw.database["users"].update_one(
        {"mobile_number": "9000000111"}, {"$set": {"status": "blocked"}}))
    r = pw.login(email, password)
    assert r.status_code == 401, r.text


def test_half_a_credential_is_rejected(pw):
    for body in ({"email": "a@b.com"}, {"password": "hunter2hunter2"}, {}):
        r = pw.client.post("/api/v1/auth/signin", json=body)
        assert r.status_code == 422, (body, r.text)


def test_password_signin_is_rate_limited(pw, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "PASSWORD_LOGIN_MAX_PER_EMAIL_PER_HOUR", 3)
    _, email, _ = pw.active_user()
    codes = [pw.login(email, "wrong-every-time").status_code for _ in range(5)]
    assert 429 in codes, codes


def test_resend_otp_still_requires_a_mobile_number(pw):
    r = pw.client.post("/api/v1/auth/resend-otp", json={"email": "a@b.com", "password": "x" * 10})
    assert r.status_code == 422, r.text
