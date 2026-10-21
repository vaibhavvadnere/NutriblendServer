"""
Favourites: the ♡ on videos in the app.

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
def fav(monkeypatch):
    from mongomock_motor import AsyncMongoMockClient

    from app.core import deps
    from app.core.config import settings
    from app.repositories import (
        favourite_repo, otp_repo, rate_limit_repo, refresh_token_repo, subscription_repo, user_repo, video_repo,
        watch_repo,
    )

    db = AsyncMongoMockClient()["favourites_test"]
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
    monkeypatch.setattr(settings, "FAVOURITES_MAX_PER_USER", 500)
    deps.admin_mobile_numbers.cache_clear()

    now = datetime.now(timezone.utc)

    async def setup():
        await db["favourites"].create_index([("user_id", 1), ("video_id", 1)], unique=True)
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

        def add(self, video_id, headers=None):
            return self.client.put(f"/api/v1/videos/{video_id}/favourite", headers=headers or self.user)

        def remove(self, video_id, headers=None):
            return self.client.delete(f"/api/v1/videos/{video_id}/favourite", headers=headers or self.user)

        def mine(self, headers=None, **params):
            return self.client.get("/api/v1/users/me/favourites", headers=headers or self.user, params=params)

        def counts(self, headers=None):
            """The ♡ block of the combined stats endpoint."""
            return self.client.get("/api/v1/users/me/stats", headers=headers or self.user)

        def count(self, **query):
            return asyncio.run(db["favourites"].count_documents(query))

    return H()


def _ids(response):
    return [v["id"] for v in response.json()["data"]["items"]]


# ── Adding and removing ──────────────────────────────────────────────────────

def test_add_and_remove_are_idempotent(fav):
    v = fav.video()
    r = fav.add(v)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data == {"video_id": v, "is_favourite": True, "favourites_count": 1, "favourites_limit": 500}
    assert fav.add(v).json()["data"]["favourites_count"] == 1          # twice: still one, no error
    assert fav.count() == 1

    r = fav.remove(v)
    assert r.status_code == 200 and r.json()["data"]["is_favourite"] is False
    assert r.json()["data"]["favourites_count"] == 0
    assert fav.remove(v).status_code == 200                            # twice: harmless
    assert fav.count() == 0


def test_only_visible_videos_can_be_added(fav):
    draft = fav.video("Draft", status="draft")
    doc_pending = fav.video("Doc", document={"status": "processing"})
    for vid in (draft, doc_pending, str(ObjectId()), "not-an-id"):
        r = fav.add(vid)
        assert r.status_code == 404 and r.json()["error"]["code"] == "VIDEO_NOT_FOUND", (vid, r.text)
    assert fav.count() == 0


def test_removing_never_checks_visibility(fav):
    v = fav.video()
    fav.add(v)
    asyncio.run(fav.database["videos"].update_one({"_id": ObjectId(v)}, {"$set": {"status": "draft"}}))
    assert fav.remove(v).status_code == 200 and fav.count() == 0
    assert fav.remove("not-an-id").json()["error"]["code"] == "VIDEO_NOT_FOUND"


def test_limit(fav):
    fav.settings_.FAVOURITES_MAX_PER_USER = 2
    a, b, c = fav.video("A"), fav.video("B"), fav.video("C")
    assert fav.add(a).status_code == 200 and fav.add(b).status_code == 200
    r = fav.add(c)
    assert r.status_code == 409 and r.json()["error"]["code"] == "FAVOURITES_LIMIT_REACHED"
    assert r.json()["error"]["details"]["limit"] == 2
    assert fav.add(a).status_code == 200                                # re-adding an existing one is still fine
    fav.remove(a)
    assert fav.add(c).status_code == 200


def test_rate_limit(fav):
    fav.settings_.RATE_LIMIT_ENABLED = True
    fav.settings_.FAVOURITES_MAX_CHANGES_PER_HOUR = 3
    v = fav.video()
    for _ in range(3):
        assert fav.add(v).status_code == 200
    r = fav.remove(v)
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    assert fav.add(v, headers=fav.other).status_code == 200            # counted per user


# ── Who may use it ───────────────────────────────────────────────────────────

def test_needs_an_app_session(fav):
    v = fav.video()
    assert fav.client.put(f"/api/v1/videos/{v}/favourite").status_code == 401
    r = fav.add(v, headers=fav.admin)
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRONG_SESSION_TYPE"
    assert fav.mine(headers=fav.admin).status_code == 403


def test_favourites_are_per_user(fav):
    v = fav.video()
    fav.add(v)
    assert _ids(fav.mine()) == [v]
    assert _ids(fav.mine(headers=fav.other)) == []
    assert fav.client.get(f"/api/v1/videos/{v}", headers=fav.other).json()["data"]["is_favourite"] is False


# ── The list and is_favourite ────────────────────────────────────────────────

def test_list_newest_first_with_pages(fav):
    vids = [fav.video(f"V{i}") for i in range(5)]
    for v in vids:
        fav.add(v)
    first = fav.mine(page=1, page_size=2).json()["data"]
    assert first["total"] == 5 and first["pages"] == 3
    assert [v["id"] for v in first["items"]] == [vids[4], vids[3]]
    assert all(v["is_favourite"] and v["playback_url"] for v in first["items"])
    assert _ids(fav.mine(page=3, page_size=2)) == [vids[0]]
    assert _ids(fav.mine(page=9, page_size=2)) == []


def test_unpublished_video_leaves_the_list_and_comes_back(fav):
    a, b = fav.video("A"), fav.video("B")
    fav.add(a)
    fav.add(b)
    set_status = lambda s: asyncio.run(fav.database["videos"].update_one({"_id": ObjectId(a)}, {"$set": {"status": s}}))
    set_status("draft")
    data = fav.mine().json()["data"]
    assert [v["id"] for v in data["items"]] == [b] and data["total"] == 1
    assert fav.count() == 2                                             # kept, only hidden
    set_status("published")
    assert _ids(fav.mine()) == [b, a]


def test_is_favourite_on_video_list_and_detail(fav):
    a, b = fav.video("A"), fav.video("B")
    fav.add(a)
    items = {v["id"]: v["is_favourite"] for v in fav.client.get("/api/v1/videos", headers=fav.user).json()["data"]["items"]}
    assert items == {a: True, b: False}
    assert fav.client.get(f"/api/v1/videos/{a}", headers=fav.user).json()["data"]["is_favourite"] is True
    assert fav.client.get(f"/api/v1/videos/{b}", headers=fav.user).json()["data"]["is_favourite"] is False


def test_video_lock(fav):
    """Hearts work while videos are locked; the list (playback links) needs a subscription."""
    v = fav.video()
    fav.settings_.PAYMENTS_LOCK_VIDEOS = True
    assert fav.add(v).status_code == 200
    r = fav.mine()
    assert r.status_code == 403 and r.json()["error"]["code"] == "SUBSCRIPTION_REQUIRED"
    assert fav.remove(v).status_code == 200


# ── Clean-up ─────────────────────────────────────────────────────────────────

def test_deleting_a_video_deletes_its_favourites(fav, monkeypatch):
    from app.services import video_service

    async def no_files(doc):
        return None

    monkeypatch.setattr(video_service, "_delete_files", no_files)
    a, b = fav.video("A"), fav.video("B")
    fav.add(a)
    fav.add(a, headers=fav.other)
    fav.add(b)
    r = fav.client.delete(f"/api/v1/admin/videos/{a}", headers=fav.admin)
    assert r.status_code == 200, r.text
    assert fav.count(video_id=ObjectId(a)) == 0
    assert fav.count(video_id=ObjectId(b)) == 1


def test_deleting_the_account_deletes_its_favourites(fav):
    v = fav.video()
    fav.add(v)
    fav.add(v, headers=fav.other)
    step1 = fav.client.post("/api/v1/auth/delete-account", json={"mobile_number": USER_MOBILE})
    otp = step1.json()["data"].get("dev_otp")
    assert otp, step1.text
    step2 = fav.client.post("/api/v1/auth/delete-account", json={"mobile_number": USER_MOBILE, "otp": otp})
    assert step2.json()["data"]["deleted"] is True
    assert fav.count() == 1                                             # only the other user's is left


# ── The count endpoint ───────────────────────────────────────────────────────

def test_count_is_zero_for_a_new_user(fav):
    r = fav.counts()
    assert r.status_code == 200, r.text
    assert r.json()["data"]["favourites"] == {"count": 0, "visible_count": 0, "limit": 500}


def test_count_tracks_adds_and_removes(fav):
    a, b = fav.video("A"), fav.video("B")
    fav.add(a); fav.add(b)
    assert fav.counts().json()["data"]["favourites"]["count"] == 2
    assert fav.counts().json()["data"]["favourites"]["visible_count"] == 2

    fav.remove(a)
    data = fav.counts().json()["data"]["favourites"]
    assert data["count"] == 1 and data["visible_count"] == 1


def test_count_agrees_with_the_add_response(fav):
    v = fav.video()
    added = fav.add(v).json()["data"]
    assert fav.counts().json()["data"]["favourites"]["count"] == added["favourites_count"]
    assert fav.counts().json()["data"]["favourites"]["limit"] == added["favourites_limit"]


def test_unpublished_favourite_is_kept_but_not_visible(fav):
    """The two counts exist precisely for this case."""
    a, b = fav.video("A"), fav.video("B")
    fav.add(a); fav.add(b)
    asyncio.run(fav.database["videos"].update_one({"title": "A"}, {"$set": {"status": "draft"}}))

    data = fav.counts().json()["data"]["favourites"]
    assert data["count"] == 2, "the favourite itself is kept"
    assert data["visible_count"] == 1, "but the app can only show one"
    assert data["visible_count"] == fav.mine().json()["data"]["total"], "must match the list"


def test_count_is_per_user(fav):
    v = fav.video()
    fav.add(v)
    assert fav.counts().json()["data"]["favourites"]["count"] == 1
    assert fav.counts(headers=fav.other).json()["data"]["favourites"]["count"] == 0


def test_count_needs_a_signed_in_app_user(fav):
    assert fav.client.get("/api/v1/users/me/stats").status_code == 401
    r = fav.counts(headers=fav.admin)       # an admin-scope session is the wrong type here
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRONG_SESSION_TYPE", r.text


def test_count_works_without_a_subscription(fav, monkeypatch):
    """The list is locked behind a subscription; the badge must not be."""
    v = fav.video()
    fav.add(v)
    monkeypatch.setattr(fav.settings_, "PAYMENTS_LOCK_VIDEOS", True)

    assert fav.mine().status_code == 403, "the list still requires a subscription"
    r = fav.counts()
    assert r.status_code == 200, r.text
    assert r.json()["data"]["favourites"]["count"] == 1
