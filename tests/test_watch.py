"""
Watch history: recording plays, the app's watched count, and the two admin views.

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
def w(monkeypatch):
    from mongomock_motor import AsyncMongoMockClient

    from app.core import deps
    from app.core.config import settings
    from app.repositories import (
        favourite_repo, otp_repo, rate_limit_repo, refresh_token_repo, subscription_repo, user_repo, video_repo,
        watch_repo,
    )

    db = AsyncMongoMockClient()["watch_test"]
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

        def play(self, video_id, headers=None):
            return self.client.post(f"/api/v1/videos/{video_id}/watch", headers=headers or self.user)

        def my_count(self, headers=None):
            """The watch block of the combined stats endpoint."""
            return self.client.get("/api/v1/users/me/stats", headers=headers or self.user)

        def ranking(self, headers=None, **params):
            return self.client.get("/api/v1/admin/stats/most-watched",
                                   headers=headers or self.admin, params=params)

        def history(self, mobile, headers=None, **params):
            uid = asyncio.run(db["users"].find_one({"mobile_number": mobile}))["_id"]
            return self.client.get(f"/api/v1/admin/users/{uid}/watched",
                                   headers=headers or self.admin, params=params)

        def rows(self, **query):
            return asyncio.run(db["watch_history"].count_documents(query))

        def age_last_play(self, seconds):
            """Pretend the last play happened `seconds` ago, so the dedupe window has passed."""
            from datetime import timedelta
            asyncio.run(db["watch_history"].update_many(
                {}, {"$set": {"last_watched_at": datetime.now(timezone.utc) - timedelta(seconds=seconds)}}))

    return H()




# ── Recording a play ─────────────────────────────────────────────────────────

def test_first_play_creates_one_row(w):
    v = w.video()
    r = w.play(v)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"video_id": v, "play_count": 1, "watched_videos_count": 1}
    assert w.rows() == 1


def test_replaying_within_the_dedupe_window_does_not_count_again(w):
    """A seek, a re-buffer or a rotation makes the app post again — one viewing."""
    v = w.video()
    w.play(v)
    for _ in range(5):
        r = w.play(v)
    assert r.json()["data"]["play_count"] == 1, "still one play"
    assert r.json()["data"]["watched_videos_count"] == 1
    assert w.rows() == 1, "and still one row"


def test_replaying_later_counts_as_a_new_play(w):
    v = w.video()
    w.play(v)
    w.age_last_play(600)                      # 10 minutes ago, past the 5-minute window
    r = w.play(v)
    assert r.json()["data"]["play_count"] == 2
    assert r.json()["data"]["watched_videos_count"] == 1, "same video — videos watched is still 1"


def test_watched_count_counts_videos_not_plays(w):
    a, b = w.video("A"), w.video("B")
    w.play(a); w.age_last_play(600); w.play(a)     # twice
    w.play(b)
    data = w.my_count().json()["data"]["watched"]
    assert data == {"videos_count": 2, "total_plays": 3}


def test_only_visible_videos_can_be_recorded(w):
    draft = w.video("Draft", status="draft")
    for vid in (draft, str(ObjectId()), "not-an-id"):
        r = w.play(vid)
        assert r.status_code == 404 and r.json()["error"]["code"] == "VIDEO_NOT_FOUND", (vid, r.text)
    assert w.rows() == 0


def test_history_is_per_user(w):
    v = w.video()
    w.play(v)
    assert w.my_count().json()["data"]["watched"]["videos_count"] == 1
    assert w.my_count(headers=w.other).json()["data"]["watched"]["videos_count"] == 0


def test_recording_needs_an_app_session(w):
    v = w.video()
    assert w.client.post(f"/api/v1/videos/{v}/watch").status_code == 401
    r = w.play(v, headers=w.admin)
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRONG_SESSION_TYPE", r.text


def test_count_is_zero_for_a_new_user(w):
    assert w.my_count().json()["data"]["watched"] == {"videos_count": 0, "total_plays": 0}


# ── Dashboard: most watched ──────────────────────────────────────────────────

def test_ranking_is_by_viewers_not_plays(w):
    """One enthusiast must not outrank two separate people."""
    popular, replayed = w.video("Popular"), w.video("Replayed")
    w.play(popular); w.play(popular, headers=w.other)          # 2 viewers, 2 plays
    for _ in range(4):                                          # 1 viewer, 4 plays
        w.play(replayed); w.age_last_play(600)

    items = w.ranking().json()["data"]["items"]
    assert [i["title"] for i in items] == ["Popular", "Replayed"], items
    assert items[0]["viewers"] == 2 and items[0]["plays"] == 2
    assert items[1]["viewers"] == 1 and items[1]["plays"] >= 4


def test_ranking_omits_videos_nobody_played(w):
    seen, unseen = w.video("Seen"), w.video("Unseen")
    w.play(seen)
    r = w.ranking()
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == 1 and [i["title"] for i in data["items"]] == ["Seen"]


def test_ranking_needs_an_admin_session(w):
    assert w.client.get("/api/v1/admin/stats/most-watched").status_code == 401
    r = w.ranking(headers=w.user)
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRONG_SESSION_TYPE", r.text


# ── Dashboard: one user's history ────────────────────────────────────────────

def test_user_history_newest_first_with_video_details(w):
    a, b = w.video("A", status="published"), w.video("B")
    w.play(a); w.age_last_play(600); w.play(b)

    data = w.history(USER_MOBILE).json()["data"]
    assert data["total"] == 2
    assert [i["title"] for i in data["items"]] == ["B", "A"], "most recently played first"
    first = data["items"][0]
    assert first["play_count"] == 1 and first["status"] == "published"
    assert first["first_watched_at"] and first["last_watched_at"]


def test_user_history_keeps_a_deleted_video_as_a_blank_row(w):
    v = w.video("Gone")
    w.play(v)
    asyncio.run(w.database["videos"].delete_one({"title": "Gone"}))
    item = w.history(USER_MOBILE).json()["data"]["items"][0]
    assert item["video_id"] == v and item["title"] is None and item["status"] is None


def test_user_history_needs_an_admin_session(w):
    r = w.history(USER_MOBILE, headers=w.user)
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRONG_SESSION_TYPE", r.text


# ── Clean-up ─────────────────────────────────────────────────────────────────

def test_deleting_a_video_removes_its_watch_rows(w):
    from app.repositories import watch_repo
    v = w.video()
    w.play(v)
    assert w.rows() == 1
    asyncio.run(watch_repo.delete_for_video(ObjectId(v)))
    assert w.rows() == 0
    assert w.my_count().json()["data"]["watched"]["videos_count"] == 0


def test_deleting_a_user_removes_their_watch_rows(w):
    from app.repositories import watch_repo
    v = w.video()
    w.play(v)
    uid = asyncio.run(w.database["users"].find_one({"mobile_number": USER_MOBILE}))["_id"]
    asyncio.run(watch_repo.delete_for_user(uid))
    assert w.rows() == 0


# ── The combined endpoint ────────────────────────────────────────────────────

def test_stats_returns_both_blocks_in_one_call(w):
    """One call fills the profile screen: ♡ numbers and watch numbers."""
    v = w.video()
    w.play(v)
    w.client.put(f"/api/v1/videos/{v}/favourite", headers=w.user)

    r = w.client.get("/api/v1/users/me/stats", headers=w.user)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert set(data) == {"favourites", "watched"}
    assert data["favourites"] == {"count": 1, "visible_count": 1, "limit": 500}
    assert data["watched"] == {"videos_count": 1, "total_plays": 1}


def test_stats_needs_an_app_session(w):
    assert w.client.get("/api/v1/users/me/stats").status_code == 401
    r = w.client.get("/api/v1/users/me/stats", headers=w.admin)
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRONG_SESSION_TYPE", r.text


def test_the_old_count_endpoints_are_gone(w):
    for path in ("/api/v1/users/me/watched/count", "/api/v1/users/me/favourites/count"):
        r = w.client.get(path, headers=w.user)
        assert r.status_code == 404, (path, r.status_code)
