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


def video_server(req):
    path = req.url.path
    rest = path[len("/api/v1/admin/videos"):]
    if req.method == "GET" and rest == "":
        items = list(VIDEOS.values())
        return ok({"items": items, "total": len(items), "page": 1, "page_size": 20, "pages": 1})
    if rest == "/categories":
        return ok({"categories": ["Recipes", "Workouts"]})
    if req.method == "POST" and rest == "":
        body = json.loads(req.content)
        VIDEOS["new"] = video_doc("new", "uploading", title=body["title"], file_size=body["file_size"],
                                  upload={"chunk_size": 1000, "total_chunks": -(-body["file_size"] // 1000),
                                          "received_chunks": 0, "missing_chunks": list(range(-(-body["file_size"] // 1000))),
                                          "complete": False, "expires_at": "2026-09-29T10:00:00Z"})
        UPLOADED["new"] = {}
        return ok(VIDEOS["new"])
    parts = rest.strip("/").split("/")
    vid = parts[0]
    if len(parts) == 4 and parts[1] == "upload" and parts[2] == "chunks":
        UPLOADED.setdefault(vid, {})[int(parts[3])] = req.content
        up = VIDEOS[vid]["upload"]
        return ok({**up, "received_chunks": len(UPLOADED[vid]),
                   "missing_chunks": [i for i in range(up["total_chunks"]) if i not in UPLOADED[vid]]})
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
        VIDEOS[vid]["status"] = json.loads(req.content)["status"]
        return ok(VIDEOS[vid])
    if len(parts) == 1 and req.method == "GET":
        return ok(VIDEOS[vid])
    return httpx.Response(404, json={"success": False, "message": "no", "error": {"code": "NOT_FOUND"}})


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    VIDEOS.clear()
    UPLOADED.clear()
    DOCS.clear()
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
{page}.render()
"""
    at = AppTest.from_string(script, default_timeout=15)
    for k, v in (query or {}).items():
        at.query_params[k] = v
    return at.run()


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


def test_videos_list():
    at = run_page("videos")
    assert not at.exception, at.exception
    df = at.dataframe[0].value
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


def test_video_detail_uploading_offers_resume():
    at = run_page("video_detail", {"id": "v3"})
    assert not at.exception, at.exception
    assert any("isn't finished" in w.value and "40%" in w.value for w in at.warning)
    assert any(b.label == "Resume upload" for b in at.button)


def test_upload_page_from_mac_path(tmp_path):
    data = b"\x00\x00\x00\x18ftypmp42" + b"v" * 2488   # 2500 bytes -> 3 chunks
    f = tmp_path / "lesson.mp4"
    f.write_bytes(data)
    at = run_page("video_upload")
    assert not at.exception, at.exception
    at.text_input(key="upload.1.title").input("Lesson 1")
    at.selectbox(key="upload.1.source.folder").select("Other folder…").run()
    at.text_input(key="upload.1.source.other").input(str(tmp_path)).run()
    assert at.selectbox(key="upload.1.source.video").value is None      # nothing pre-selected
    at.selectbox(key="upload.1.source.video").select_index(0).run()
    assert at.selectbox(key="upload.1.source.video").value.name == "lesson.mp4"
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    assert b"".join(UPLOADED["new"][i] for i in sorted(UPLOADED["new"])) == data
    assert VIDEOS["new"]["status"] == "draft"
    assert any("Lesson 1" in s.value and "uploaded" in s.value for s in at.success)


def test_upload_page_requires_title():
    at = run_page("video_upload")
    at.button(key="upload.1.go").click().run()
    assert any("Enter a title" in e.value for e in at.error)


def test_upload_page_paste_path(tmp_path):
    f = tmp_path / "Amino Acid 35_ Liquid Formulation.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 100)
    at = run_page("video_upload")
    at.selectbox(key="upload.1.source.folder").select("Paste a full path…").run()
    at.text_input(key="upload.1.source.path").input(f"file://{str(f).replace(' ', '%20')}").run()
    assert not at.error, [e.value for e in at.error]
    assert any("Selected: Amino Acid 35_ Liquid Formulation.mp4" in c.value for c in at.caption)


def test_upload_with_document(tmp_path):
    video = b"\x00\x00\x00\x18ftypmp42" + b"v" * 1500
    (tmp_path / "lesson.mp4").write_bytes(video)
    pdf = b"%PDF-1.4 recipe sheet"
    (tmp_path / "Recipe Sheet.pdf").write_bytes(pdf)
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("Lesson with doc")
    at.selectbox(key="upload.1.source.folder").select("Other folder…").run()
    at.text_input(key="upload.1.source.other").input(str(tmp_path)).run()
    at.selectbox(key="upload.1.source.video").select_index(0).run()
    at.checkbox(key="upload.1.attach_doc").check().run()
    at.selectbox(key="upload.1.doc.folder").select("Other folder…").run()
    at.text_input(key="upload.1.doc.other").input(str(tmp_path)).run()
    doc_box = at.selectbox(key="upload.1.doc.video")
    assert doc_box.value is None and [o for o in doc_box.options] and len(doc_box.options) == 1   # only documents listed
    doc_box.select_index(0).run()
    assert at.selectbox(key="upload.1.doc.video").value.name == "Recipe Sheet.pdf"
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    assert DOCS["new"] == {"name": "Recipe Sheet.pdf", "bytes": pdf}
    assert any("Recipe Sheet.pdf" in c.value and "view-only" in c.value for c in at.caption)


def test_attach_doc_ticked_but_not_chosen():
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input("x")
    at.selectbox(key="upload.1.source.folder").select("Paste a full path…").run()
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
    assert list(at.dataframe[0].value["Document"]) == ["PDF", "—", "—"]


def _upload_ok(tmp_path, title="Lesson 1"):
    (tmp_path / "lesson.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 1500)
    at = run_page("video_upload")
    at.text_input(key="upload.1.title").input(title)
    at.text_area(key="upload.1.description").input("Some notes")
    at.selectbox(key="upload.1.source.folder").select("Other folder…").run()
    at.text_input(key="upload.1.source.other").input(str(tmp_path)).run()
    at.selectbox(key="upload.1.source.video").select_index(0).run()
    at.button(key="upload.1.go").click().run()
    assert not at.exception, at.exception
    return at


def test_fields_reset_after_successful_upload(tmp_path):
    at = _upload_ok(tmp_path)
    # success banner stays, every field is fresh (new form version) and empty
    assert any("Lesson 1" in s.value and "uploaded" in s.value for s in at.success)
    assert at.text_input(key="upload.2.title").value == ""
    assert at.text_area(key="upload.2.description").value == ""
    assert at.selectbox(key="upload.2.source.folder").value is None
    assert "upload.1.title" not in at.session_state
    # the banner can open the new video or be dismissed
    assert any(b.label == "Open video" for b in at.button)
    next(b for b in at.button if b.label == "Dismiss").click().run()
    assert not at.success


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
    assert not at.success


def test_nothing_preselected_on_arrival():
    at = run_page("video_upload")
    assert at.selectbox(key="upload.1.source.folder").value is None
    at.checkbox(key="upload.1.attach_doc").check().run()
    assert at.selectbox(key="upload.1.doc.folder").value is None
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
