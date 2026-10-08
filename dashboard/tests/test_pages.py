"""
Screen tests for Dashboard, Users and User details (AppTest + fake server).
Each test starts already signed in, then renders one page.
"""

import json
from datetime import date, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from dashboard.auth import session

ADMIN = {"id": "a1", "mobile_number": "8484844053", "name": "Vaibhav", "status": "active",
         "is_verified": True, "created_at": "2026-09-20T10:00:00Z"}


def make_users(n):
    return [
        {"id": f"u{i}", "mobile_number": f"91234{i:05d}", "name": f"User {i}", "email": None,
         "state": "Maharashtra" if i % 2 else None, "status": ["active", "pending", "blocked"][i % 3],
         "is_verified": i % 3 == 0, "created_at": "2026-09-25T10:00:00Z", "last_login_at": None}
        for i in range(n)
    ]


USERS = make_users(45)
STATE = {"u1": "pending"}
CALLS = []


def ok(data):
    return httpx.Response(200, json={"success": True, "message": "ok", "data": data})


def fake_server(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    CALLS.append((req.method, path, dict(parse_qs(req.url.query.decode()))))
    if path == "/api/v1/admin/stats":
        today = date(2026, 9, 27)
        series = [{"date": (today - timedelta(days=29 - i)).isoformat(), "count": [0, 1, 3, 2][i % 4]} for i in range(30)]
        return ok({"videos": {"uploading": 1, "draft": 1, "published": 1}, "total_users": 45, "users_by_status": {"active": 15, "pending": 15, "blocked": 15},
                   "signups_today": 2, "signups_last_7_days": 9, "active_last_7_days": 5,
                   "signups_by_day": series, "timezone": "Asia/Kolkata"})
    if path == "/api/health":
        return ok({"status": "ok", "app": "Nutriblend API", "env": "development", "database": "ok", "sms_provider": "mock"})
    if path == "/api/v1/users":
        q = parse_qs(req.url.query.decode())
        items = USERS
        if "q" in q:
            items = [u for u in items if q["q"][0].lower() in u["name"].lower()]
        if "status" in q:
            items = [u for u in items if u["status"] == q["status"][0]]
        page, size = int(q["page"][0]), int(q["page_size"][0])
        pages = max((len(items) + size - 1) // size, 1)
        return ok({"items": items[(page - 1) * size: page * size], "total": len(items),
                   "page": page, "page_size": size, "pages": pages})
    if path == "/api/v1/legal/privacy-policy":
        assert "authorization" not in req.headers   # public endpoint
        return ok({"markdown": "# Privacy Policy — Nutriblend\n\n## 2. The data we collect\n\n| Data | Why |\n|---|---|\n| Mobile number | Sign in |",
                   "version": "abc123", "effective_date": None,
                   "policy_url": "http://test/privacy-policy", "deletion_url": "http://test/account-deletion",
                   "missing_fields": ["Privacy contact email", "Effective date"],
                   "play_store_checklist": [
                       {"item": "Privacy policy page exists (HTML, not a PDF)", "ok": True, "hint": "Served."},
                       {"item": "Account deletion inside the app", "ok": False, "hint": "Not built yet."}]})
    if path.startswith("/api/v1/admin/videos"):
        return video_server(req)
    if path.startswith("/api/v1/admin/users/"):
        uid = path.split("/")[5]
        if uid == "missing":
            return httpx.Response(404, json={"success": False, "message": "User not found", "error": {"code": "ACCOUNT_NOT_FOUND"}})
        base = next(u for u in USERS if u["id"] == uid)
        if req.method == "PATCH":
            STATE[uid] = json.loads(req.content)["status"]
        return ok({**base, "status": STATE.get(uid, base["status"]), "role": "user", "active_sessions": 2, "is_admin": False})
    return httpx.Response(404, json={"success": False, "message": "no", "error": {"code": "NOT_FOUND"}})


def video_doc(vid, status="draft", **extra):
    doc = {"id": vid, "title": f"Video {vid}", "description": "desc", "category": "Recipes", "sort_order": 0,
           "duration_seconds": 125.0, "file_size": 5_000_000, "content_type": "video/mp4", "status": status,
           "original_file_name": f"{vid}.mp4", "video_codec": "h264", "has_thumbnail": False,
           "created_at": "2026-09-27T10:00:00Z", "updated_at": "2026-09-27T10:00:00Z",
           "playback_url": None if status == "uploading" else f"http://test/media/videos/{vid}.mp4?exp=1&sig=x"}
    if status == "uploading":
        doc["upload"] = {"chunk_size": 1000, "total_chunks": 10, "received_chunks": 4,
                         "missing_chunks": [4, 5, 6, 7, 8, 9], "complete": False, "expires_at": "2026-09-29T10:00:00Z"}
    doc.update(extra)
    return doc


VIDEOS = {}
UPLOADED = {}
DOCS = {}
DELETED = []


KNOWN_SHA: dict = {}
STARTS: list = []
THUMBS: dict = {}
REPLACES: list = []
RETRIES: list = []
OPTIMIZATION = {"enabled": False, "available": False, "active": False, "crf": 21, "preset": "fast", "skip_below_kbps": 4000}


def video_server(req):
    path = req.url.path
    rest = path[len("/api/v1/admin/videos"):]
    if req.method == "GET" and rest == "":
        items = [v for v in VIDEOS.values() if not v.get("replaces")]      # unfinished replacements aren't listed
        return ok({"items": items, "total": len(items), "page": 1, "page_size": 20, "pages": 1})
    if rest == "/storage-check":
        broken = [v for v in VIDEOS.values() if v.get("missing_files")]
        return ok({"storage": "r2", "checked": len(VIDEOS), "errors": 0,
                   "broken": [{"id": v["id"], "title": v["title"], "status": v["status"],
                               "created_at": v["created_at"], "missing_files": v["missing_files"]} for v in broken]})
    if req.method == "DELETE" and rest.count("/") == 1:
        DELETED.append(rest.strip("/"))
        gone = VIDEOS.pop(rest.strip("/"), None)
        if gone and gone.get("replaces") in VIDEOS:
            VIDEOS[gone["replaces"]].pop("replacement", None)
        return ok({})
    if rest == "/categories":
        return ok({"categories": ["Recipes", "Workouts"]})
    if rest == "/optimization":
        return ok(OPTIMIZATION)
    if req.method == "POST" and rest.endswith("/optimization/retry"):
        vid = rest.split("/")[1]
        RETRIES.append(vid)
        VIDEOS[vid]["optimization"] = {"state": "queued", "progress": 0.0, "source_size": 1000}
        return ok(VIDEOS[vid])
    if rest == "/check-details":
        import difflib
        q = dict(req.url.params)
        norm = lambda t: " ".join((t or "").split()).casefold()
        same = [{"id": v["id"], "title": v["title"], "status": v["status"], "created_at": v["created_at"]}
                for v in VIDEOS.values() if q.get("title") and norm(v["title"]) == norm(q["title"])
                and v["id"] != q.get("exclude_id")]
        names = {norm(n): n for n in ["Recipes", "Workouts"]}
        cat = {"exists": False, "canonical": None, "similar": []}
        if q.get("category"):
            k = norm(q["category"])
            if k in names:
                cat.update(exists=True, canonical=names[k])
            else:
                cat["similar"] = [names[m] for m in difflib.get_close_matches(k, list(names), n=3, cutoff=0.75)]
        CALLS.append(("check-details", q))
        return ok({"same_title": same, "category": cat})
    if req.method == "POST" and rest == "":
        body = json.loads(req.content)
        STARTS.append(body)
        if body.get("sha256") in KNOWN_SHA and not body.get("allow_duplicate"):
            return httpx.Response(409, json={"success": False, "message": "This exact video file has already been uploaded.",
                                             "error": {"code": "DUPLICATE_VIDEO", "details": KNOWN_SHA[body["sha256"]]}})
        VIDEOS["new"] = video_doc("new", "uploading", title=body["title"], file_size=body["file_size"],
                                  upload={"chunk_size": 1000, "total_chunks": -(-body["file_size"] // 1000),
                                          "received_chunks": 0, "missing_chunks": list(range(-(-body["file_size"] // 1000))),
                                          "complete": False, "expires_at": "2026-09-29T10:00:00Z"})
        UPLOADED["new"] = {}
        return ok(VIDEOS["new"])
    parts = rest.strip("/").split("/")
    vid = parts[0]
    if req.method == "POST" and parts[1:] == ["replace-file"]:
        body = json.loads(req.content)
        REPLACES.append({"video": vid, **body})
        if body.get("sha256") in KNOWN_SHA and not body.get("allow_duplicate"):
            return httpx.Response(409, json={"success": False, "message": "This exact video file has already been uploaded.",
                                             "error": {"code": "DUPLICATE_VIDEO", "details": KNOWN_SHA[body["sha256"]]}})
        stg = f"stg{len(REPLACES)}"
        n = -(-body["file_size"] // 1000)
        VIDEOS[stg] = video_doc(stg, "uploading", title=f"{VIDEOS[vid]['title']} (new file)", file_size=body["file_size"],
                                original_file_name=body["file_name"], replaces=vid,
                                upload={"chunk_size": 1000, "total_chunks": n, "received_chunks": 0,
                                        "missing_chunks": list(range(n)), "complete": False, "expires_at": "2026-09-29T10:00:00Z"})
        VIDEOS[vid]["replacement"] = {"video_id": stg, "file_name": body["file_name"], "file_size": body["file_size"],
                                      "started_at": "2026-09-28T10:00:00Z"}
        UPLOADED[stg] = {}
        return ok(VIDEOS[stg])
    if len(parts) == 4 and parts[1] == "upload" and parts[2] == "chunks":
        import base64, hashlib
        assert req.headers["content-md5"] == base64.b64encode(hashlib.md5(req.content).digest()).decode()
        UPLOADED.setdefault(vid, {})[int(parts[3])] = req.content
        up = VIDEOS[vid]["upload"]
        return ok({**up, "received_chunks": len(UPLOADED[vid]),
                   "missing_chunks": [i for i in range(up["total_chunks"]) if i not in UPLOADED[vid]]})
    if parts[1:] == ["thumbnail"] and req.method == "PUT":
        body = req.content
        THUMBS[vid] = {"content_type": req.headers["content-type"], "has_jpeg": b"\xff\xd8" in body, "size": len(body)}
        VIDEOS[vid]["has_thumbnail"] = True
        return ok(VIDEOS[vid])
    if parts[1:] == ["document", "ticket"]:
        return ok({"video_id": vid, "ticket": f"doc-ticket-{vid}", "expires_at": "2026-09-29T22:00:00Z"})
    if parts[1:] == ["upload", "ticket"]:
        return ok({"video_id": vid, "ticket": f"ticket-{vid}", "expires_at": "2026-09-29T22:00:00Z"})
    if parts[1:] == ["upload"] and req.method == "GET":
        up = VIDEOS[vid]["upload"]
        got = UPLOADED.get(vid, {})
        missing = [i for i in range(up["total_chunks"]) if i not in got]
        return ok({**up, "received_chunks": len(got), "missing_chunks": missing, "complete": not missing})
    if parts[1:] == ["upload", "complete"]:
        VIDEOS[vid] = video_doc(vid, "draft", title=VIDEOS[vid]["title"])
        return ok(VIDEOS[vid])
    if parts[1:] == ["document"] and req.method == "PUT":
        from urllib.parse import unquote
        body = b"".join(req.stream) if hasattr(req, "stream") else req.content
        DOCS[vid] = {"name": unquote(req.headers["x-file-name"]), "bytes": req.read()}
        VIDEOS[vid]["admin_document"] = {"name": DOCS[vid]["name"], "file_type": DOCS[vid]["name"].rsplit(".", 1)[1],
                                         "size": len(DOCS[vid]["bytes"]), "page_count": 2, "status": "ready",
                                         "uploaded_at": "2026-09-29T10:00:00Z"}
        return ok(VIDEOS[vid])
    if parts[1:] == ["document"] and req.method == "DELETE":
        VIDEOS[vid].pop("admin_document", None)
        return ok(VIDEOS[vid])
    if parts[1:] == ["document", "pages"]:
        return ok({"name": "x.pdf", "page_count": 2, "pages": ["http://test/p1.jpg", "http://test/p2.jpg"],
                   "links_expire_at": "2026-09-29T11:00:00Z"})
    if parts[1:] == ["status"]:
        body = json.loads(req.content)
        v = VIDEOS[vid]
        doc = v.get("admin_document")
        opt = (v.get("optimization") or {}).get("state")
        if body["status"] == "published" and v["status"] != "published" and opt in ("queued", "running", "failed"):
            if opt == "failed" or not body.get("when_ready"):               # the server's rule
                return httpx.Response(409, json={"success": False, "message": "Still being optimized.",
                                                 "error": {"code": "VIDEO_OPTIMIZING", "details": {"optimization_state": opt}}})
            v["publish_when_ready"] = True
            return ok(v)
        if body["status"] == "published" and v["status"] != "published" and doc and doc["status"] != "ready":
            if doc["status"] == "failed" or not body.get("when_ready"):    # the server's rule
                return httpx.Response(409, json={"success": False, "message": "The document isn't ready.",
                                                 "error": {"code": "DOCUMENT_NOT_READY", "details": {"document_status": doc["status"]}}})
            v["publish_when_ready"] = True
            return ok(v)
        v["status"] = body["status"]
        v.pop("publish_when_ready", None)
        v["visible_in_app"] = v["status"] == "published" and not (doc and doc["status"] != "ready")
        return ok(v)
    if len(parts) == 1 and req.method == "GET":
        return ok(VIDEOS[vid])
    return httpx.Response(404, json={"success": False, "message": "no", "error": {"code": "NOT_FOUND"}})


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    VIDEOS.clear()
    KNOWN_SHA.clear()
    STARTS.clear()
    THUMBS.clear()
    REPLACES.clear()
    RETRIES.clear()
    OPTIMIZATION.update(enabled=False, available=False, active=False)
    UPLOADED.clear()
    DOCS.clear()
    DELETED.clear()
    VIDEOS["v1"] = video_doc("v1", "published")
    VIDEOS["v2"] = video_doc("v2", "draft")
    VIDEOS["v3"] = video_doc("v3", "uploading")
    CALLS.clear()
    STATE.clear()
    STATE["u1"] = "pending"
    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(fake_server))
    monkeypatch.setattr(session, "_shared_http", lambda: client)


URLS = {"privacy": "privacy", "video_upload": "upload", "overview": "dashboard", "users": "users", "user_detail": "user",
        "videos": "videos", "video_detail": "video"}


def run_page(page: str, query=None) -> AppTest:
    script = f"""
import time
import streamlit as st
from dashboard.auth import session
from dashboard.models import User
from dashboard.pages import {page}
if "auth.refresh_token" not in st.session_state:
    session.SessionTokenStore().set_tokens("a" * 30, "r" * 30, 900)
    session.set_current_user(User.model_validate({ADMIN!r}))
from dashboard import navigation
class _Here:
    url_path = st.session_state.get("_test_page", {URLS.get(page, page)!r})
navigation.track(_Here())
from dashboard.components import browser_upload as _box
def _fake_box(key, **kw):   # stands in for the in-browser upload box
    slot = "_doc_last" if kw.get("kind") == "document" else "_box_last"
    st.session_state[slot] = dict(key=key, **kw)
    ev = st.session_state.get("_box_event")
    return ev if ev and ev.get("key") == key else None
_box.render = _fake_box
def _fake_manager(tasks):   # stands in for the sidebar upload manager (as main.py runs it)
    st.session_state["_mgr_last"] = tasks
    return st.session_state.get("_mgr_event")
_box.manager = _fake_manager
from dashboard.components import video_table as _vt
def _fake_table(key, rows):   # stands in for the clickable-row table
    st.session_state["_table_last"] = dict(key=key, rows=rows)
    ev = st.session_state.get("_table_event")
    return ev if ev and ev.get("key", key) == key else None
_vt.render = _fake_table
from dashboard.services import uploads as _uploads
with st.sidebar:
    _uploads.handle(_box.new_event(_box.manager(_uploads.manager_defs()), _uploads.SEEN))
    _uploads.sidebar_notices()
if st.session_state.get("_test_delete"):
    from dashboard.pages import videos as _videos
    _videos.delete_records(st.session_state.pop("_test_delete"))
if _Here.url_path == {URLS.get(page, page)!r}:
    {page}.render()
else:
    st.write("another page")   # the admin is elsewhere; the sidebar still runs
"""
    at = AppTest.from_string(script, default_timeout=15)
    for k, v in (query or {}).items():
        at.query_params[k] = v
    return at.run()


def table_df(at):
    """The Videos list as a DataFrame (rows the page handed to the clickable table)."""
    import pandas as pd
    return pd.DataFrame(at.session_state["_table_last"]["rows"])


def test_overview_tiles_and_chart():
    at = run_page("overview")
    assert not at.exception, at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Total users"] == "45" and metrics["Blocked"] == "15"
    assert metrics["Signups today"] == "2" and metrics["Signed in, last 7 days"] == "5"
    assert any("Signups per day" in s.value for s in at.subheader)
    assert any("busiest day" in c.value for c in at.caption)


def test_users_list_pagination_and_filters():
    at = run_page("users")
    assert not at.exception, at.exception
    assert len(at.dataframe[0].value) == 20
    assert any("of 45 users" in c.value and "page 1 of 3" in c.value for c in at.caption)
    next(b for b in at.button if b.label == "Next").click().run()
    assert any("21–40 of 45" in c.value for c in at.caption)
    at.selectbox(key="users.status").select("Blocked").run()
    assert any("of 15 users" in c.value and "page 1 of 1" in c.value for c in at.caption)
    at.text_input(key="users.q").input("nobody").run()
    assert any("No users match" in i.value for i in at.info)


def test_user_detail_and_block_unblock():
    at = run_page("user_detail", {"id": "u1"})
    assert not at.exception, at.exception
    assert at.title[0].value == "User 1"
    assert any(b.label == "Block user" for b in at.button)
    # Unblock path: make u1 blocked first
    STATE["u1"] = "blocked"
    at.run()
    next(b for b in at.button if b.label == "Unblock user").click().run()
    assert ("PATCH", "/api/v1/admin/users/u1/status", {}) in CALLS
    assert any("unblocked" in s.value for s in at.success)
    assert STATE["u1"] == "active"


def test_user_detail_not_found():
    at = run_page("user_detail", {"id": "missing"})
    assert not at.exception
    assert any("User not found" in e.value for e in at.error)


def test_overview_video_tiles():
    at = run_page("overview")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Published"] == "1" and metrics["Uploads in progress"] == "1"


def test_videos_row_click_opens_video_once():
    at = run_page("videos")
    assert not at.exception, at.exception
    last = at.session_state["_table_last"]
    vid = last["rows"][0]["id"]
    at.session_state["_table_event"] = {"seq": 1, "id": vid}
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["videos.table_seen"] == 1
    # coming back to the list with the same stale event must not open it again
    at2 = run_page("videos")
    at2.session_state["videos.table_seen"] = 1
    at2.session_state["_table_event"] = {"seq": 1, "id": vid}
    at2.run()
    assert not at2.exception, at2.exception
    assert len(table_df(at2)) == 3


def test_videos_list():
    at = run_page("videos")
    assert not at.exception, at.exception
    df = table_df(at)
    assert list(df["Status"]) == ["Published", "Draft", "Uploading (40%)"]
    assert list(df["Duration"])[0] == "2:05" and list(df["Size"])[0] == "4.8 MB"
    assert any(b.label == "Upload video" for b in at.button)


def test_video_detail_publish():
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert at.title[0].value == "Video v2"
    next(b for b in at.button if b.label == "Publish").click().run()
    assert VIDEOS["v2"]["status"] == "published"
    assert any("published" in s.value for s in at.success)


def _with_document(vid, status, **video_fields):
    VIDEOS[vid]["admin_document"] = {"name": "Guide.pptx", "file_type": "pptx", "size": 2048,
                                     "page_count": None if status != "ready" else 2, "status": status,
                                     "uploaded_at": "2026-09-29T10:00:00Z"}
    VIDEOS[vid].update(video_fields)


def test_publish_while_document_is_preparing_waits_for_it():
    _with_document("v2", "processing")
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Publish when the document is ready").click().run()
    assert not at.exception, at.exception
    assert VIDEOS["v2"]["status"] == "draft" and VIDEOS["v2"]["publish_when_ready"] is True
    assert any("published automatically" in s.value for s in at.success)
    assert any("Waiting for the document" in i.value for i in at.info)       # the page now says so
    assert not any(b.label == "Publish" for b in at.button)
    next(b for b in at.button if b.label == "Cancel (keep as draft)").click().run()
    assert VIDEOS["v2"]["status"] == "draft" and "publish_when_ready" not in VIDEOS["v2"]
    assert any(b.label == "Publish when the document is ready" for b in at.button)


def test_failed_document_blocks_publishing():
    _with_document("v2", "failed")
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any("couldn't be prepared" in w.value for w in at.warning)
    assert next(b for b in at.button if b.label == "Publish").disabled


def test_ready_document_publishes_normally():
    _with_document("v2", "ready")
    at = run_page("video_detail", {"id": "v2"})
    next(b for b in at.button if b.label == "Publish").click().run()
    assert VIDEOS["v2"]["status"] == "published"
    assert any("visible in the app" in s.value for s in at.success)


def test_published_video_with_unready_document_is_shown_as_hidden():
    _with_document("v1", "processing", visible_in_app=False)
    at = run_page("video_detail", {"id": "v1"})
    assert not at.exception, at.exception
    assert any("hidden from the app" in w.value and "still being prepared" in w.value for w in at.warning)
    assert any(b.label == "Unpublish (make draft)" for b in at.button)
    at = run_page("videos")
    assert "Published · hidden (document not ready)" in list(table_df(at)["Status"])


def test_videos_list_marks_drafts_that_publish_when_ready():
    VIDEOS["v2"]["publish_when_ready"] = True
    at = run_page("videos")
    assert "Draft · publishes when ready" in list(table_df(at)["Status"])


def test_video_detail_uploading_offers_resume():
    at = run_page("video_detail", {"id": "v3"})
    assert not at.exception, at.exception
    assert any("isn't finished" in w.value and "40%" in w.value for w in at.warning)
    assert any(b.label == "Resume upload" for b in at.button)


import hashlib as _hashlib
import itertools as _itertools

_SEQ = _itertools.count(1)
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"v" * 2488      # 2500 bytes -> 3 chunks of 1000


def box_event(at, doc=False, **event):
    """Make a (fake) page box report an event, like the real one does."""
    box = at.session_state["_doc_last" if doc else "_box_last"]
    at.session_state["_box_event"] = {"key": box["key"], "seq": f"t{next(_SEQ)}", **event}
    return at.run()


def choose(at, data=MP4, name="lesson.mp4", hashed=True, mismatch=False, probe=None):
    extra = {"probe": probe} if probe is not None else {}
    return box_event(at, event="file", name=name, size=len(data), last_modified=1, mismatch=mismatch,
                     sha256=_hashlib.sha256(data).hexdigest() if hashed else None, **extra)


def choose_doc(at, data=b"%PDF-1.4 recipe sheet", name="Recipe Sheet.pdf", hashed=True):
    return box_event(at, doc=True, event="file", name=name, size=len(data), last_modified=1, mismatch=False,
                     sha256=_hashlib.sha256(data).hexdigest() if hashed else None)


def hand_over(at, *roles):
    """The page's boxes hand their files to the sidebar manager (it acknowledges)."""
    for role in roles:
        box = at.session_state["_doc_last" if role == "document" else "_box_last"]
        assert box["task"] and box["task"]["role"] == role, f"no task given to the {role} box"
        box_event(at, doc=role == "document", event="handed", task=box["task"]["id"], role=role)
    return at


def manager_event(at, **event):
    at.session_state["_mgr_event"] = {"seq": f"m{next(_SEQ)}", **event}
    return at.run()


def manager_runs(at, data=MP4, doc=None, doc_status="processing", doc_error=None, index=-1):
    """What the real sidebar manager does with a task: upload, then report "done"."""
    task = at.session_state["_mgr_last"][index]
    vid = (task["video_job"] or task["doc_job"])["video_id"]
    if task["video_job"]:
        job = task["video_job"]
        assert job["ticket"] == f"ticket-{vid}" and job["api"].endswith("/api/v1") and job["parallel"] == 4
        UPLOADED[vid] = {i: data[i * job["chunk_size"]:(i + 1) * job["chunk_size"]] for i in range(job["total_chunks"])}
        VIDEOS[vid] = video_doc(vid, "draft", title=VIDEOS[vid]["title"])
    if task["doc_job"] and not doc_error:
        assert task["doc_job"]["ticket"] == f"doc-ticket-{vid}"
        name, content = doc or ("Recipe Sheet.pdf", b"%PDF-1.4 recipe sheet")
        DOCS[vid] = {"name": name, "bytes": content}
        VIDEOS[vid]["admin_document"] = {"name": name, "file_type": name.rsplit(".", 1)[1], "size": len(content),
                                         "page_count": None if doc_status == "processing" else 2, "status": doc_status,
                                         "uploaded_at": "2026-09-29T10:00:00Z"}
    return manager_event(at, event="done", task=task["id"], video=VIDEOS[vid], doc_error=doc_error)


def test_upload_page_browser_upload():
    at = run_page("video_upload")
    assert not at.exception, at.exception
    assert at.session_state["_box_last"]["task"] is None and at.session_state["_box_last"]["max_bytes"] == 4096 * 1024 * 1024
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    assert STARTS[-1]["sha256"] == _hashlib.sha256(MP4).hexdigest() and STARTS[-1]["file_name"] == "lesson.mp4"
    assert at.session_state["_mgr_last"][0]["video_job"]["video_id"] == "new"     # the sidebar has the task
    assert any("Starting the upload" in i.value for i in at.info)
    hand_over(at, "video")
    assert not at.exception, at.exception
    # the form is free again while the sidebar uploads
    assert any("Lesson 1" in i.value and "is uploading" in i.value for i in at.info)
    assert at.text_input(key="upload.2.title").value == "" and at.session_state["_box_last"]["task"] is None
    manager_runs(at)
    assert not at.exception, at.exception
    assert b"".join(UPLOADED["new"][i] for i in sorted(UPLOADED["new"])) == MP4
    assert any("Lesson 1" in s.value and "uploaded" in s.value for s in at.success)
    assert at.session_state["uploads.tasks"] == {}                                   # task finished


GOOD_PROBE = {"readable": True, "duration": 754.3, "width": 1920, "height": 1080, "video_codec": "H.264",
              "video_profile": "High", "audio_codec": "AAC", "fast_start": True, "bitrate": 5_000_000, "warnings": []}


def test_what_the_browser_read_from_the_video_goes_to_the_server():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at, probe=GOOD_PROBE)
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    assert STARTS[-1]["duration_seconds"] == 754.3 and STARTS[-1]["video_codec"] == "h264"


def test_upload_without_probe_sends_no_video_facts():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at, probe={"readable": False, "duration": None, "video_codec": None, "warnings": []})
    at.button(key="upload.1.go").click().run()
    assert "duration_seconds" not in STARTS[-1] and "video_codec" not in STARTS[-1]


def test_warnings_alone_do_not_block_the_upload():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    warn = {"code": "slow_start", "level": "warn", "text": "not fast-start"}
    choose(at, probe={**GOOD_PROBE, "fast_start": False, "warnings": [warn]})
    assert not [c for c in at.checkbox if "anyway" in c.label], "a slow-start file only warns (in the box)"
    assert not at.button(key="upload.1.go").disabled


def test_broken_video_needs_a_confirmation_before_uploading():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    bad = {"code": "truncated", "level": "error", "text": "cut short"}
    choose(at, probe={"readable": True, "duration": 3.0, "video_codec": "H.264", "warnings": [bad]})
    assert at.button(key="upload.1.go").disabled
    size = len(MP4)
    at.checkbox(key=f"upload.1.accept_bad.lesson.mp4.{size}").check().run()
    assert not at.button(key="upload.1.go").disabled
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    assert STARTS[-1]["file_name"] == "lesson.mp4"
    # choosing another (fine) file clears the block
    at2 = run_page("video_upload")
    choose(at2, probe={"readable": True, "warnings": [bad]})
    choose(at2, data=MP4 + b"x", name="other.mp4", probe=GOOD_PROBE)
    assert not at2.button(key="upload.1.go").disabled


JPEG = b"\xff\xd8\xff\xe0" + b"j" * 800


def frame_event(at, data=MP4, name="lesson.mp4", jpeg=JPEG, prefix="data:image/jpeg;base64,"):
    import base64
    return box_event(at, event="frame", name=name, size=len(data), width=960, height=540,
                     data=(prefix + base64.b64encode(jpeg).decode()) if jpeg is not None else None)


def test_cover_picture_from_the_video_becomes_the_thumbnail():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    frame_event(at)
    assert any("picture taken from the video" in c.value for c in at.caption)
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    hand_over(at, "video")
    manager_runs(at)
    assert not at.exception, at.exception
    assert THUMBS["new"]["has_jpeg"] and THUMBS["new"]["content_type"].startswith("multipart/form-data")
    assert any("Thumbnail saved" in c.value for c in at.caption)


def test_lost_fingerprint_event_is_recovered_from_the_frame_event():
    """A slow rerun can swallow the "file" event that carries the fingerprint; the frame event repeats it."""
    import base64
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at, hashed=False)                              # only the "still checking" event arrived
    box_event(at, event="frame", name="lesson.mp4", size=len(MP4), last_modified=1, mismatch=False,
              sha256=_hashlib.sha256(MP4).hexdigest(), width=960, height=540,
              data="data:image/jpeg;base64," + base64.b64encode(JPEG).decode())
    assert not at.exception, at.exception
    at.button(key="upload.1.go").click().run()
    assert not any("Still checking the file" in i.value for i in at.info)
    assert not at.exception, at.exception
    assert at.session_state["_box_last"]["task"], "the upload should have started"


def test_cover_picture_sent_together_with_the_fingerprint():
    import base64
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    box_event(at, event="file", name="lesson.mp4", size=len(MP4), last_modified=1, mismatch=False,
              sha256=_hashlib.sha256(MP4).hexdigest(),
              cover={"data": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode(), "width": 960, "height": 540})
    assert any("picture taken from the video" in c.value for c in at.caption)
    at.button(key="upload.1.go").click().run()
    assert not any("Still checking the file" in i.value for i in at.info)
    assert at.session_state["_box_last"]["task"]
    hand_over(at, "video")
    manager_runs(at)
    assert THUMBS["new"]["has_jpeg"]


def test_a_click_survives_a_page_restart():
    """Editing another field right after clicking Upload restarts the page; the click must not be lost."""
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    at.session_state["upload.1.go_pending"] = True          # what the button's callback leaves behind
    at.run()                                                 # a restart without the click itself
    assert not at.exception, at.exception
    assert at.session_state["_box_last"]["task"], "the remembered click should have started the upload"
    assert "upload.1.go_pending" not in at.session_state


def test_click_before_the_file_check_ends_starts_by_itself():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at, hashed=False)
    at.button(key="upload.1.go").click().run()
    assert any("Still checking the file" in i.value for i in at.info)
    assert not at.session_state["_box_last"]["task"]
    choose(at)                                               # the fingerprint arrives
    assert at.session_state["_box_last"]["task"], "the upload should start once the check is done"


def test_remembered_click_is_dropped_when_the_form_is_not_valid():
    at = run_page("video_upload")
    at.session_state["upload.1.go_pending"] = True
    at.run()
    assert any("Enter a title" in e.value for e in at.error)
    assert "upload.1.go_pending" not in at.session_state


def test_no_cover_when_the_browser_could_not_decode_the_video():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    frame_event(at, jpeg=None)                                      # e.g. HEVC in a browser without a decoder
    assert not any("picture taken from the video" in c.value for c in at.caption)
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    manager_runs(at)
    assert not THUMBS


def test_cover_of_another_file_is_not_used():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    frame_event(at)
    choose(at, data=MP4 + b"x", name="other.mp4")                   # picked a different video: old cover is dropped
    assert not any("picture taken from the video" in c.value for c in at.caption)
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    manager_runs(at, MP4 + b"x")
    assert not THUMBS


def test_cover_picture_is_checked_before_use():
    from dashboard.components.video_ui import frame_thumbnail
    import base64
    chosen = {"name": "a.mp4", "size": 10}
    good = {"name": "a.mp4", "size": 10, "data": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()}
    assert frame_thumbnail(good, chosen) == ("cover.jpg", JPEG, "image/jpeg")
    assert frame_thumbnail({**good, "size": 11}, chosen) is None                      # other file
    assert frame_thumbnail({**good, "data": "data:image/png;base64,AAAA"}, chosen) is None
    assert frame_thumbnail({**good, "data": "data:image/jpeg;base64,!!!"}, chosen) is None
    assert frame_thumbnail({**good, "data": "data:image/jpeg;base64," + base64.b64encode(b"GIF89a" + b"x" * 600).decode()}, chosen) is None
    assert frame_thumbnail({**good, "data": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8" + b"x" * (6 * 1024 * 1024)).decode()}, chosen) is None
    assert frame_thumbnail(None, chosen) is None and frame_thumbnail(good, None) is None


def manager_swaps(at, data=MP4, index=-1):
    """What the sidebar manager and the server do when a replacement upload completes: the new file becomes
    the video's file, the unfinished upload disappears, the manager reports the *video*."""
    task = at.session_state["_mgr_last"][index]
    stg = task["video_job"]["video_id"]
    target = VIDEOS[stg]["replaces"]
    job = task["video_job"]
    assert job["ticket"] == f"ticket-{stg}"
    UPLOADED[stg] = {i: data[i * job["chunk_size"]:(i + 1) * job["chunk_size"]] for i in range(job["total_chunks"])}
    VIDEOS[target].pop("replacement", None)
    VIDEOS[target].update(file_size=len(data), original_file_name=VIDEOS[stg]["original_file_name"])
    VIDEOS.pop(stg)
    return manager_event(at, event="done", task=task["id"], video=VIDEOS[target], doc_error=None)


def test_replace_video_file():
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any(h.value == "Replace video file" for h in at.subheader)
    assert at.button(key="replace.v2.go").disabled
    choose(at, data=MP4, name="better.mp4", probe=GOOD_PROBE)
    assert not at.button(key="replace.v2.go").disabled
    at.button(key="replace.v2.go").click().run()
    assert not at.exception, at.exception
    r = REPLACES[-1]
    assert r["video"] == "v2" and r["file_name"] == "better.mp4" and r["sha256"] == _hashlib.sha256(MP4).hexdigest()
    assert r["duration_seconds"] == 754.3 and r["video_codec"] == "h264" and r["allow_duplicate"] is False
    task = at.session_state["_mgr_last"][0]
    assert task["video_job"]["video_id"] == "stg1" and task["label"] == "Video v2 (new file)"
    hand_over(at, "video")
    assert not at.exception, at.exception
    assert any("Uploading" in c.value for c in at.caption)                           # the page says it's going on
    manager_swaps(at)
    assert not at.exception, at.exception
    assert any("video file of “Video v2” was replaced" in s.value for s in at.success)
    assert "replacement" not in VIDEOS["v2"] and "stg1" not in VIDEOS and at.session_state["uploads.tasks"] == {}


def test_replace_with_a_file_that_already_exists_asks_first():
    KNOWN_SHA[_hashlib.sha256(MP4).hexdigest()] = {"video_id": "v1", "title": "Original lesson", "status": "published",
                                                   "created_at": "2026-09-01T10:00:00Z", "uploaded_fraction": 1.0}
    at = run_page("video_detail", {"id": "v2"})
    choose(at)
    at.button(key="replace.v2.go").click().run()
    assert not at.exception, at.exception
    assert any("already exists as “Original lesson”" in w.value for w in at.warning)
    assert at.button(key="replace.v2.go").disabled and "replacement" not in VIDEOS["v2"]
    at.button(key="replace.v2.anyway").click().run()
    assert not at.exception, at.exception
    assert REPLACES[-1]["allow_duplicate"] is True and VIDEOS["v2"]["replacement"]["video_id"] == "stg2"


def test_replace_with_a_broken_file_needs_confirmation():
    at = run_page("video_detail", {"id": "v2"})
    bad = {"code": "truncated", "level": "error", "text": "cut short"}
    choose(at, probe={"readable": True, "duration": 3.0, "video_codec": "H.264", "warnings": [bad]})
    assert at.button(key="replace.v2.go").disabled
    at.checkbox(key=f"replace.v2.accept_bad.lesson.mp4.{len(MP4)}").check().run()
    assert not at.button(key="replace.v2.go").disabled


def _unfinished_replacement():
    VIDEOS["s1"] = video_doc("s1", "uploading", title="Video v2 (new file)", file_size=5000, original_file_name="better.mp4",
                             replaces="v2")
    VIDEOS["v2"]["replacement"] = {"video_id": "s1", "file_name": "better.mp4", "file_size": 5000,
                                   "started_at": "2026-09-28T10:00:00Z"}


def test_unfinished_replacement_can_be_resumed_or_discarded():
    _unfinished_replacement()
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any("isn't finished" in w.value for w in at.warning)
    assert "resume.s1.box" in at.session_state["_box_last"]["key"]
    assert at.session_state["_box_last"]["expect"]["name"] == "better.mp4"
    at.button(key="replace.v2.discard").click().run()
    assert not at.exception, at.exception
    assert "s1" not in VIDEOS and "replacement" not in VIDEOS["v2"] and "s1" in DELETED[-1]
    assert any("discarded" in s.value for s in at.success)


def test_resuming_a_replacement_swaps_the_file_in():
    _unfinished_replacement()
    at = run_page("video_detail", {"id": "v2"})
    data = b"\x00\x00\x00\x18ftypmp42" + b"n" * 4988                       # 5000 bytes, the file it was started with
    box_event(at, event="file", name="better.mp4", size=len(data), last_modified=1, mismatch=False,
              sha256=_hashlib.sha256(data).hexdigest())
    next(b for b in at.button if b.label == "Resume upload").click().run()
    assert not at.exception, at.exception
    assert at.session_state["_mgr_last"][0]["video_job"]["video_id"] == "s1"
    hand_over(at, "video")
    manager_swaps(at, data)
    assert any("was replaced" in s.value for s in at.success)
    assert "replacement" not in VIDEOS["v2"]


def test_unfinished_replacement_page_points_to_its_video():
    _unfinished_replacement()
    at = run_page("video_detail", {"id": "s1"})
    assert not at.exception, at.exception
    assert any("new file for “Video v2”" in i.value for i in at.info)
    assert any(b.label == "Open that video" for b in at.button)
    assert any(b.label == "Discard this upload" for b in at.button)


def test_a_lost_reply_is_not_reported_as_a_failure_when_the_swap_happened():
    at = run_page("video_detail", {"id": "v2"})
    choose(at)
    at.button(key="replace.v2.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]
    VIDEOS["v2"].pop("replacement")                                             # the server did swap, the reply was lost
    VIDEOS.pop("stg1")
    manager_event(at, event="error", task=task["id"], message="Network error", stage="video")
    assert not at.exception, at.exception
    assert any("was replaced" in s.value for s in at.success)


def test_a_real_failure_is_reported_and_kept_for_retry():
    at = run_page("video_detail", {"id": "v2"})
    choose(at)
    at.button(key="replace.v2.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]
    manager_event(at, event="error", task=task["id"], message="Network error", stage="video")
    assert not at.exception, at.exception
    assert any("failed" in w.value and "Network error" in w.value for w in at.sidebar.warning)
    assert "replacement" in VIDEOS["v2"]                                        # still pending; Retry in the sidebar
    assert at.session_state["uploads.tasks"][task["id"]]["state"] == "failed"


def test_videos_list_shows_a_pending_replacement():
    _unfinished_replacement()
    at = run_page("videos")
    assert any(s.endswith("· new file uploading") for s in table_df(at)["Status"])


def test_upload_page_requires_title():
    at = run_page("video_upload")
    at.button(key="upload.1.go").click().run()
    assert any("Enter a title" in e.value for e in at.error)


def test_upload_waits_for_fingerprint():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at, hashed=False)
    at.button(key="upload.1.go").click().run()
    assert any("Still checking the file" in i.value for i in at.info) and not STARTS


def test_failed_upload_reported_and_kept_for_retry():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]
    manager_event(at, event="error", task=task["id"], video_id="new", stage="video", message="Network error")
    assert not at.exception, at.exception
    assert any("failed: Network error" in w.value for w in at.warning)
    assert any("Network error" in w.value for w in at.sidebar.warning)          # note in the sidebar, every page
    assert at.session_state["_mgr_last"][0]["id"] == task["id"]                 # still there: Retry in the sidebar
    manager_runs(at)                                                             # retried and finished
    assert any("uploaded" in s.value for s in at.success) and at.session_state["uploads.tasks"] == {}


def test_upload_keeps_going_on_another_page():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    at.checkbox(key="upload.1.publish").check().run()
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    at.session_state["_test_page"] = "videos"          # the admin moves on…
    at.run()
    manager_runs(at)                                    # …the sidebar finishes the upload
    assert not at.exception, at.exception
    assert VIDEOS["new"]["status"] == "published"      # follow-ups ran even though another page was open
    assert any("Lesson 1" in m.value for m in at.sidebar.success)                # note in the sidebar
    next(b for b in at.sidebar.button if b.label == "OK").click().run()
    assert not at.sidebar.success
    at.session_state["_test_page"] = "upload"          # back on the Upload page: the result is shown
    at.run()
    assert any("Lesson 1" in s.value and "published" in s.value for s in at.success)


def test_upload_with_document():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson with doc")
    choose(at)
    at.checkbox(key="upload.1.attach_doc").check().run()
    assert at.session_state["_doc_last"]["kind"] == "document" and at.session_state["_doc_last"]["task"] is None
    choose_doc(at)
    at.checkbox(key="upload.1.publish").check().run()
    at.button(key="upload.1.go").click().run()
    task = at.session_state["_mgr_last"][0]
    assert task["video_job"]["video_id"] == "new" and task["doc_job"]["video_id"] == "new"
    assert any("Starting" in i.value for i in at.info)
    hand_over(at, "video")
    assert at.text_input(key="upload.1.title").value == "Lesson with doc"   # waits for the document box too
    hand_over(at, "document")
    assert at.text_input(key="upload.2.title").value == ""
    assert VIDEOS["new"]["status"] == "uploading"
    manager_runs(at)
    assert not at.exception, at.exception
    assert DOCS["new"]["name"] == "Recipe Sheet.pdf"
    assert any("Recipe Sheet.pdf" in c.value and "being prepared" in c.value for c in at.caption)
    # the document is still being converted: the video waits and goes live by itself (server side)
    assert VIDEOS["new"]["status"] == "draft" and VIDEOS["new"]["publish_when_ready"] is True
    assert any("published automatically" in s.value and "document is ready" in s.value for s in at.success)


def test_upload_with_ready_document_publishes_at_once():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson with pdf")
    choose(at)
    at.checkbox(key="upload.1.attach_doc").check().run()
    choose_doc(at)
    at.checkbox(key="upload.1.publish").check().run()
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video", "document")
    manager_runs(at, doc_status="ready")
    assert not at.exception, at.exception
    assert VIDEOS["new"]["status"] == "published" and "publish_when_ready" not in VIDEOS["new"]
    assert any("is published and visible" in s.value for s in at.success)


def test_document_failure_keeps_video_as_draft():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson")
    choose(at)
    at.checkbox(key="upload.1.attach_doc").check().run()
    choose_doc(at)
    at.checkbox(key="upload.1.publish").check().run()
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video", "document")
    manager_runs(at, doc_error="Network error")
    assert not at.exception, at.exception
    assert VIDEOS["new"]["status"] == "draft"
    assert any("document wasn't" in c.value and "Network error" in c.value for c in at.caption)


def test_attach_doc_ticked_but_not_chosen():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("x")
    at.checkbox(key="upload.1.attach_doc").check().run()
    at.button(key="upload.1.go").click().run()
    assert any("Choose a video" in e.value or "Choose the document" in e.value for e in at.error)


def test_video_detail_document_section():
    VIDEOS["v2"]["admin_document"] = {"name": "Guide.pptx", "file_type": "pptx", "size": 2048, "page_count": 2,
                                      "status": "ready", "uploaded_at": "2026-09-29T10:00:00Z"}
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any("Guide.pptx" in m.value and "PowerPoint" in m.value and "2 pages" in m.value for m in at.markdown)
    assert at.number_input(key="video_detail.doc_page.v2").value == 1
    next(b for b in at.button if b.label == "Remove document").click().run()
    assert "admin_document" not in VIDEOS["v2"]
    assert any("Document removed" in s.value for s in at.success)


def test_video_detail_document_failed_shows_retry():
    VIDEOS["v2"]["admin_document"] = {"name": "Guide.docx", "file_type": "docx", "size": 2048, "status": "failed",
                                      "error": "LibreOffice is not installed", "uploaded_at": "2026-09-29T10:00:00Z"}
    at = run_page("video_detail", {"id": "v2"})
    assert any("LibreOffice" in e.value for e in at.error)
    assert any(b.label == "Retry" for b in at.button)


def test_videos_list_document_column():
    VIDEOS["v1"]["admin_document"] = {"name": "a.pdf", "file_type": "pdf", "size": 1, "status": "ready"}
    at = run_page("videos")
    assert list(table_df(at)["Document"]) == ["PDF", "—", "—"]


def _upload_ok(tmp_path, title="Lesson 1"):
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input(title)
    at.text_area(key="upload.1.description").input("Some notes")
    choose(at)
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    manager_runs(at)
    assert not at.exception, at.exception
    return at


def test_fields_reset_after_successful_upload(tmp_path):
    at = _upload_ok(tmp_path)
    # success banner stays, every field is fresh (new form version) and empty
    assert any("Lesson 1" in s.value and "uploaded" in s.value for s in at.success)
    assert at.text_input(key="upload.2.title").value == ""
    assert at.text_area(key="upload.2.description").value == ""
    assert at.session_state["_box_last"]["key"] == "upload.2.video"     # a fresh, empty upload box
    assert at.session_state["_box_last"]["task"] is None
    assert "upload.1.title" not in at.session_state
    # the banner can open the new video or be dismissed
    assert any(b.label == "Open video" for b in at.button)
    next(b for b in at.button if b.label == "Dismiss").click().run()
    assert not at.main.success


def test_coming_back_starts_from_scratch(tmp_path):
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Half-filled")
    at.checkbox(key="upload.1.attach_doc").check().run()
    assert at.text_input(key="upload.1.title").value == "Half-filled"   # kept while you stay on the page
    at.session_state["_test_page"] = "videos"        # go to another screen…
    at.run()
    at.session_state["_test_page"] = "upload"        # …and come back
    at.run()
    title = next(t for t in at.text_input if t.label == "Title *")
    attach = next(c for c in at.checkbox if c.label.startswith("Attach a document"))
    assert title.value == "" and title.key != "upload.1.title"
    assert attach.value is False


def test_coming_back_also_clears_old_success_banner(tmp_path):
    at = _upload_ok(tmp_path)
    at.session_state["_test_page"] = "videos"
    at.run()
    at.session_state["_test_page"] = "upload"
    at.run()
    assert not at.main.success


def test_nothing_preselected_on_arrival():
    at = run_page("video_upload")
    at.checkbox(key="upload.1.attach_doc").check().run()
    assert at.session_state["_doc_last"]["key"] == "upload.1.doc"      # an empty document box
    at.button(key="upload.1.go").click().run()
    assert any("Enter a title" in e.value for e in at.error)


def test_privacy_policy_page():
    at = run_page("privacy")
    assert not at.exception, at.exception
    assert at.title[0].value == "Privacy policy"
    assert any("http://test/privacy-policy" in c.value for c in at.code)
    assert any("Google Play readiness — 1 of 2" in s.value for s in at.subheader)
    assert any("Account deletion inside the app" in m.value for m in at.markdown)
    assert any("Privacy contact email" in m.value for m in at.markdown)
    assert any("The data we collect" in m.value for m in at.markdown)


def test_videos_page_no_warning_when_storage_ok():
    at = run_page("videos")
    assert not at.exception, at.exception
    assert not any("missing from storage" in e.value for e in at.error)


def test_videos_page_flags_and_deletes_broken_videos():
    VIDEOS["v1"]["missing_files"] = ["video", "document"]
    VIDEOS["v2"]["missing_files"] = ["video"]
    at = run_page("videos")
    assert not at.exception, at.exception
    assert any("2 of 3 videos have files missing from storage (r2)" in e.value for e in at.error)
    assert any("Video v1" in m.value and "video file, document" in m.value for m in at.markdown)
    assert list(table_df(at)["Status"])[:2] == ["Published · file missing", "Draft · file missing"]
    at.button(key="videos.delete_broken").click().run()          # opens the confirm dialog
    assert any(b.label == "Delete" for b in at.button)
    assert any(m.value == "- Video v2" for m in at.markdown)       # dialog lists what will go
    # Buttons inside a dialog can't be clicked in AppTest; run what the Delete button runs:
    at.session_state["_test_delete"] = [("v1", "Video v1"), ("v2", "Video v2")]
    at.run()
    assert not at.exception, at.exception
    assert sorted(DELETED) == ["v1", "v2"]
    assert any("Deleted 2 video record(s)" in s.value for s in at.success)
    assert not any("missing from storage" in e.value for e in at.error)   # re-checked: all clean


def test_video_detail_missing_file_blocks_publish():
    VIDEOS["v2"]["missing_files"] = ["video"]
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any("Missing from storage: the video file" in e.value for e in at.error)
    assert next(b for b in at.button if b.label == "Publish").disabled
    assert any(b.label == "Delete this video" for b in at.button)
    assert any("No video to preview" in i.value for i in at.info)


def _pick_file(at, data, title):
    at.text_input(key="upload.1.title").input(title)
    return choose(at, data, name="dup.mp4")


def test_upload_duplicate_warns_then_upload_anyway(tmp_path):
    import hashlib
    data = b"\x00\x00\x00\x18ftypmp42" + b"d" * 2488
    KNOWN_SHA[hashlib.sha256(data).hexdigest()] = {"video_id": "v1", "title": "Original lesson", "status": "published",
                                                   "created_at": "2026-09-01T10:00:00Z", "uploaded_fraction": 1.0}
    at = _pick_file(run_page("video_upload"), data, "Copy")
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    assert any("already uploaded" in w.value and "Original lesson" in w.value for w in at.warning)
    assert "new" not in UPLOADED and at.button(key="upload.1.go").disabled
    assert not any(b.label == "Resume that upload" for b in at.button)       # only for unfinished uploads
    at.button(key="upload.1.dup.anyway").click().run()
    assert not at.exception, at.exception
    assert STARTS[-1]["allow_duplicate"] is True and STARTS[-1]["sha256"] == STARTS[0]["sha256"]
    hand_over(at, "video")
    manager_runs(at, data)
    assert b"".join(UPLOADED["new"][i] for i in sorted(UPLOADED["new"])) == data
    assert any("Copy" in s.value and "uploaded" in s.value for s in at.success)


def test_upload_duplicate_of_unfinished_upload_resumes_it(tmp_path):
    import hashlib
    data = b"\x00\x00\x00\x18ftypmp42" + b"r" * 2488          # 2500 bytes -> 3 chunks
    sha = hashlib.sha256(data).hexdigest()
    VIDEOS["v9"] = video_doc("v9", "uploading", title="Half done", file_size=len(data), sha256=sha,
                             upload={"chunk_size": 1000, "total_chunks": 3, "received_chunks": 1,
                                     "missing_chunks": [1, 2], "complete": False, "expires_at": "2026-09-29T10:00:00Z"})
    UPLOADED["v9"] = {0: data[:1000]}
    KNOWN_SHA[sha] = {"video_id": "v9", "title": "Half done", "status": "uploading",
                      "created_at": "2026-09-01T10:00:00Z", "uploaded_fraction": 1 / 3}
    at = _pick_file(run_page("video_upload"), data, "Half done again")
    at.button(key="upload.1.go").click().run()
    assert any("already being uploaded" in w.value and "33%" in w.value for w in at.warning)
    at.button(key="upload.1.dup.resume").click().run()
    assert not at.exception, at.exception
    assert at.session_state["_mgr_last"][0]["video_job"]["video_id"] == "v9"
    hand_over(at, "video")
    manager_runs(at, data)
    assert b"".join(UPLOADED["v9"][i] for i in sorted(UPLOADED["v9"])) == data
    assert VIDEOS["v9"]["status"] == "draft" and "new" not in UPLOADED
    assert len(STARTS) == 1                                    # no second video created


def test_video_detail_shows_integrity_verified():
    VIDEOS["v2"].update(integrity_verified=True, sha256="ab" * 32)
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any("Integrity verified" in c.value and "abababababab" in c.value for c in at.caption)


def test_video_detail_resume_through_browser():
    sha = _hashlib.sha256(MP4).hexdigest()
    VIDEOS["v9"] = video_doc("v9", "uploading", title="Half done", file_size=len(MP4), sha256=sha,
                             original_file_name="lesson.mp4",
                             upload={"chunk_size": 1000, "total_chunks": 3, "received_chunks": 1,
                                     "missing_chunks": [1, 2], "complete": False, "expires_at": "2026-09-29T10:00:00Z"})
    at = run_page("video_detail", {"id": "v9"})
    assert not at.exception, at.exception
    box = at.session_state["_box_last"]
    assert box["expect"] == {"name": "lesson.mp4", "size": len(MP4), "sha256": sha} and box["task"] is None
    assert next(b for b in at.button if b.label == "Resume upload").disabled      # no file chosen yet
    choose(at, mismatch=True)                                # the box says: not the same file
    assert next(b for b in at.button if b.label == "Resume upload").disabled
    choose(at)
    next(b for b in at.button if b.label == "Resume upload").click().run()
    assert at.session_state["_box_last"]["task"]["role"] == "video"
    assert any("Uploads" in c.value and "sidebar" in c.value for c in at.caption)
    hand_over(at, "video")
    manager_runs(at)
    assert not at.exception, at.exception
    assert any("finished uploading" in s.value for s in at.success)


def test_video_detail_document_upload_through_browser():
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    upload = next(b for b in at.button if b.label == "Upload document")
    assert upload.disabled and at.session_state["_doc_last"]["kind"] == "document"
    choose_doc(at, b"%PDF-1.7 guide", "Guide.pdf")
    next(b for b in at.button if b.label == "Upload document").click().run()
    task = at.session_state["_mgr_last"][0]
    assert task["doc_job"]["video_id"] == "v2" and task["video_job"] is None
    hand_over(at, "document")
    manager_runs(at, doc=("Guide.pdf", b"%PDF-1.7 guide"), doc_status="ready")
    assert not at.exception, at.exception
    assert any("Document uploaded" in s.value for s in at.success)
    assert DOCS["v2"]["name"] == "Guide.pdf"


def test_finished_elsewhere_is_checked_with_the_server():
    """Stop pressed during the final check: the server finished anyway, the
    browser reports "done" without a video — the real status decides."""
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]
    VIDEOS["new"] = video_doc("new", "draft", title="Lesson 1")          # finished on the server
    manager_event(at, event="done", task=task["id"], video=None, doc_error=None)
    assert not at.exception, at.exception
    assert any("Lesson 1" in s.value and "uploaded" in s.value for s in at.success)


def test_expired_upload_is_not_reported_as_success():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson 1")
    choose(at)
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]                              # VIDEOS["new"] still "uploading"
    manager_event(at, event="done", task=task["id"], video=None, doc_error=None)
    assert not at.exception, at.exception
    assert any("could not be finished" in w.value for w in at.warning)


# ── item 12: duplicate titles and category typos ────────────────────────────

def test_same_title_must_be_confirmed_before_upload():
    data = b"\x00\x00\x00\x18ftypmp42" + b"t" * 2488
    at = _pick_file(run_page("video_upload"), data, "video  V1 ")
    assert not at.exception, at.exception
    assert any("“Video v1” already exists" in w.value for w in at.warning)
    assert at.button(key="upload.1.go").disabled
    at.checkbox(key=[c.key for c in at.checkbox if "Use the same title" in c.label][0]).check().run()
    assert not at.button(key="upload.1.go").disabled
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    assert STARTS[-1]["allow_duplicate_title"] is True


def test_unique_title_shows_no_warning():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Something brand new").run()
    assert not at.warning and not any("same title" in c.label for c in at.checkbox)


def test_category_typo_is_flagged_and_can_be_fixed_with_one_click():
    at = run_page("video_upload")
    at.selectbox(key="upload.1.category.select").select("New category…").run()
    at.text_input(key="upload.1.category.new").input("Recpies").run()
    assert any("Did you mean “Recipes”" in w.value for w in at.warning)
    assert at.button(key="upload.1.go").disabled
    [b for b in at.button if b.label == "Use “Recipes”"][0].click().run()
    assert not at.exception, at.exception
    assert at.selectbox(key="upload.1.category.select").value == "Recipes"
    assert not any("Did you mean" in w.value for w in at.warning)


def test_new_looking_category_can_be_kept_with_a_tick():
    data = b"\x00\x00\x00\x18ftypmp42" + b"c" * 2488
    at = _pick_file(run_page("video_upload"), data, "Fresh title")
    at.selectbox(key="upload.1.category.select").select("New category…").run()
    at.text_input(key="upload.1.category.new").input("Recpies").run()
    assert at.button(key="upload.1.go").disabled
    [c for c in at.checkbox if "is correct" in c.label][0].check().run()
    assert not at.exception, at.exception
    assert not at.button(key="upload.1.go").disabled
    at.button(key="upload.1.go").click().run()
    assert STARTS[-1]["category"] == "Recpies"


def test_category_in_other_capitals_is_announced():
    at = run_page("video_upload")
    at.selectbox(key="upload.1.category.select").select("New category…").run()
    at.text_input(key="upload.1.category.new").input("recipes").run()
    assert any("saved as “Recipes”" in i.value for i in at.info)
    assert not at.warning


def test_renaming_to_a_used_title_needs_a_tick():
    at = run_page("video_detail", {"id": "v2"})
    at.text_input(key="edit.v2.title").input("Video v1").run()
    assert any("already exists" in w.value for w in at.warning)
    assert at.button(key="edit.v2.save").disabled
    at.text_input(key="edit.v2.title").input("VIDEO V2").run()          # only capitals of its own title
    assert not at.warning and not at.button(key="edit.v2.save").disabled


# ── video optimization (the server shrinks the file; the original is never stored in the cloud) ──────────

def _optimizing(vid="v2", state="running", progress=0.4, **extra):
    VIDEOS[vid].update(playback_url=None, optimization={"state": state, "progress": progress, "source_size": 800_000_000, **extra})


def test_optimizing_video_shows_progress_and_has_no_preview():
    _optimizing()
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any("Optimizing" in i.value and "40%" in i.value for i in at.info)
    assert any("No preview yet" in i.value for i in at.info)
    assert any(b.label == "Publish when optimized" for b in at.button)
    assert not any(b.label == "Publish" for b in at.button)


def test_publish_while_optimizing_waits_for_it():
    _optimizing(progress=0.1)
    at = run_page("video_detail", {"id": "v2"})
    next(b for b in at.button if b.label == "Publish when optimized").click().run()
    assert not at.exception, at.exception
    assert VIDEOS["v2"]["status"] == "draft" and VIDEOS["v2"]["publish_when_ready"] is True
    assert any("published automatically as soon as it is optimized" in s.value for s in at.success)
    assert any("Waiting until it is optimized" in i.value for i in at.info)


def test_failed_optimization_shows_the_reason_and_retry():
    _optimizing(state="failed", progress=0.0, error="The video could not be read: moov atom not found")
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any("couldn't be optimized" in e.value and "moov atom not found" in e.value for e in at.error)
    assert any("not sent to cloud storage" in c.value for c in at.caption)
    assert next(b for b in at.button if b.label == "Publish").disabled
    next(b for b in at.button if b.label == "Retry optimization").click().run()
    assert not at.exception, at.exception
    assert RETRIES == ["v2"] and VIDEOS["v2"]["optimization"]["state"] == "queued"


def test_finished_optimization_shows_before_and_after():
    VIDEOS["v2"].update(optimization={"state": "done", "progress": 1.0, "source_size": 800_000_000,
                                      "output_size": 150_000_000, "saved_percent": 81, "mode": "compressed"},
                        integrity_verified=True, sha256="ab" * 32)
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    text = " ".join(c.value for c in at.caption)
    assert "Optimized: 762.9 MB uploaded → 143.1 MB stored (81% smaller)" in text and "original was not stored" in text
    assert "Integrity verified" not in text                          # the stored file isn't the one that was sent
    assert any(b.label == "Publish" for b in at.button)


def test_kept_as_uploaded_is_explained():
    VIDEOS["v2"].update(optimization={"state": "done", "source_size": 60_000_000, "output_size": 60_000_000, "mode": "kept"})
    at = run_page("video_detail", {"id": "v2"})
    assert any("Stored as uploaded" in c.value and "already efficient" in c.value for c in at.caption)


def test_videos_list_shows_optimization_state():
    _optimizing("v2", progress=0.4)
    VIDEOS["v1"].update(optimization={"state": "failed", "error": "x"})
    at = run_page("videos")
    status = list(table_df(at)["Status"])
    assert "Draft · optimizing 40%" in status and "Published · hidden (document not ready)" not in status
    assert any(s.startswith("Published") for s in status) or "Draft · optimization failed" in status


def test_upload_page_explains_optimization_when_on():
    OPTIMIZATION.update(enabled=True, available=True, active=True)
    at = run_page("video_upload")
    assert not at.exception, at.exception
    assert any("Automatic optimization is on" in c.value and "original is not kept" in c.value for c in at.caption)


def test_upload_page_warns_when_ffmpeg_is_missing():
    OPTIMIZATION.update(enabled=True, available=False, active=False)
    at = run_page("video_upload")
    assert any("ffmpeg is not installed" in w.value for w in at.warning)


def test_upload_page_is_quiet_when_optimization_is_off():
    at = run_page("video_upload")
    assert not any("optimization" in c.value.lower() for c in at.caption) and not at.warning


def test_upload_with_publish_waits_for_optimization():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Big lesson")
    choose(at)
    at.checkbox(key="upload.1.publish").check().run()
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]
    vid = task["video_job"]["video_id"]
    UPLOADED[vid] = {}
    VIDEOS[vid] = video_doc(vid, "draft", title="Big lesson", playback_url=None,
                            optimization={"state": "queued", "progress": 0.0, "source_size": 2500})
    manager_event(at, event="done", task=task["id"], video=VIDEOS[vid])
    assert not at.exception, at.exception
    assert VIDEOS[vid]["status"] == "draft" and VIDEOS[vid]["publish_when_ready"] is True
    assert any("published automatically as soon as it is optimized" in s.value for s in at.success)


def test_upload_without_publish_says_it_is_optimizing():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Big lesson")
    choose(at)
    at.button(key="upload.1.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]
    vid = task["video_job"]["video_id"]
    VIDEOS[vid] = video_doc(vid, "draft", title="Big lesson", playback_url=None,
                            optimization={"state": "queued", "progress": 0.0, "source_size": 2500})
    manager_event(at, event="done", task=task["id"], video=VIDEOS[vid])
    assert any("now being optimized into a smaller file" in s.value for s in at.success)


def test_replacement_being_optimized_is_explained_and_not_an_error():
    VIDEOS["v2"]["replacement"] = {"video_id": "stg2", "file_name": "new.mp4", "file_size": 5000,
                                   "started_at": "2026-09-27T10:00:00Z", "optimization": "running"}
    VIDEOS["stg2"] = video_doc("stg2", "draft", replaces="v2", playback_url=None,
                               optimization={"state": "running", "progress": 0.5, "source_size": 5000})
    at = run_page("video_detail", {"id": "v2"})
    assert not at.exception, at.exception
    assert any("the new file is being converted" in i.value and "50%" in i.value for i in at.info)
    assert any("being optimized" in c.value and "replaces the current file automatically" in c.value for c in at.caption)
    assert not any(b.label == "Replace video file" for b in at.button)       # nothing more to do until it is swapped in


def test_failed_replacement_can_be_retried_or_discarded():
    VIDEOS["v2"]["replacement"] = {"video_id": "stg2", "file_name": "new.mp4", "file_size": 5000,
                                   "started_at": "2026-09-27T10:00:00Z", "optimization": "failed"}
    VIDEOS["stg2"] = video_doc("stg2", "draft", replaces="v2", playback_url=None,
                               optimization={"state": "failed", "error": "bad file", "source_size": 5000})
    at = run_page("video_detail", {"id": "v2"})
    assert any("The new file couldn't be optimized" in e.value and "bad file" in e.value for e in at.error)
    next(b for b in at.button if b.label == "Retry optimization").click().run()
    assert RETRIES == ["stg2"]
    VIDEOS["stg2"]["optimization"]["state"] = "failed"
    at = run_page("video_detail", {"id": "v2"})
    next(b for b in at.button if b.label == "Discard the new file").click().run()
    assert "stg2" in DELETED and "replacement" not in VIDEOS["v2"]


def test_replace_upload_received_then_optimized_is_reported_as_received():
    at = run_page("video_detail", {"id": "v2"})
    choose(at, data=MP4, name="better.mp4", probe=GOOD_PROBE)
    at.button(key="replace.v2.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]
    # the server has the whole file and is optimizing it: the video points at the unfinished upload
    VIDEOS["v2"]["replacement"] = {"video_id": "stg1", "file_name": "better.mp4", "file_size": len(MP4),
                                   "started_at": "2026-09-27T10:00:00Z", "optimization": "queued"}
    VIDEOS["stg1"] = video_doc("stg1", "draft", replaces="v2", playback_url=None,
                               optimization={"state": "queued", "source_size": len(MP4)})
    manager_event(at, event="done", task=task["id"], video=VIDEOS["stg1"], doc_error=None)
    assert not at.exception, at.exception
    assert any("was received and is being optimized" in s.value and "keeps playing" in s.value for s in at.success)
    assert not any("was replaced" in s.value for s in at.success)


def test_replace_reply_lost_but_new_file_received_counts_as_done():
    at = run_page("video_detail", {"id": "v2"})
    choose(at, data=MP4, name="better.mp4", probe=GOOD_PROBE)
    at.button(key="replace.v2.go").click().run()
    hand_over(at, "video")
    task = at.session_state["_mgr_last"][0]
    VIDEOS["v2"]["replacement"] = {"video_id": "stg1", "file_name": "better.mp4", "file_size": len(MP4),
                                   "started_at": "2026-09-27T10:00:00Z", "optimization": "running"}
    manager_event(at, event="error", task=task["id"], message="network")      # the 'complete' reply never arrived
    assert not at.exception, at.exception
    assert not any("failed" in w.value for w in at.warning)
    assert any("being optimized" in s.value for s in at.success)
