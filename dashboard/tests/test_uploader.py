"""Resumable uploader against a fake server that speaks the chunk protocol."""

import io
import json
import math

import httpx
import pytest

from dashboard.api.client import ApiClient, MemoryTokenStore
from dashboard.services import uploader

CHUNK = 1000
FILE = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 20  # 5132 bytes -> 6 chunks


def _md5(b):
    import base64, hashlib
    return base64.b64encode(hashlib.md5(b).digest()).decode()


def _err(code, http_status, message="no", **details):
    return httpx.Response(http_status, json={"success": False, "message": message,
                                        "error": {"code": code, "details": details}})


class FakeServer:
    """Checks Content-MD5 like the real server. corrupt_once: chunks whose bytes
    get damaged on the way the first time. bad_at_complete: chunks the final
    verification finds wrong (once). known: sha256 -> existing video."""

    def __init__(self, fail_once=(), reject_chunk=None, corrupt_once=(), bad_at_complete=(), known=None,
                 sha_mismatch_at_complete=False):
        self.corrupt_once = set(corrupt_once)
        self.bad_at_complete = set(bad_at_complete)
        self.known = known or {}
        self.sha_mismatch_at_complete = sha_mismatch_at_complete
        self.completes = 0
        self.video = None
        self.received = {}
        self.fail_once = set(fail_once)
        self.reject_chunk = reject_chunk
        self.completed = False
        self.puts = []

    def _video(self, status="uploading"):
        total = math.ceil(self.video["file_size"] / CHUNK)
        missing = [i for i in range(total) if i not in self.received]
        return {
            "id": "v1", "title": self.video["title"], "file_size": self.video["file_size"], "status": status,
            "created_at": "2026-09-28T10:00:00Z",
            "upload": None if status != "uploading" else {
                "chunk_size": CHUNK, "total_chunks": total, "received_chunks": len(self.received),
                "missing_chunks": missing, "complete": not missing, "expires_at": "2026-09-29T10:00:00Z"},
        }

    def __call__(self, req: httpx.Request) -> httpx.Response:
        ok = lambda d, code=200: httpx.Response(code, json={"success": True, "message": "ok", "data": d})
        path = req.url.path
        if req.method == "POST" and path == "/api/v1/admin/videos":
            self.video = json.loads(req.content)
            sha = self.video.get("sha256")
            if sha in self.known and not self.video.get("allow_duplicate"):
                return _err("DUPLICATE_VIDEO", 409, "This exact video file has already been uploaded.", **self.known[sha])
            return ok(self._video(), 201)
        if req.method == "PUT" and "/upload/chunks/" in path:
            i = int(path.rsplit("/", 1)[1])
            self.puts.append(i)
            if i in self.fail_once:
                self.fail_once.discard(i)
                return httpx.Response(502, json={"success": False, "message": "bad gateway", "error": {"code": "INTERNAL_ERROR"}})
            if i == self.reject_chunk:
                return httpx.Response(422, json={"success": False, "message": "This file is not a valid MP4 video.", "error": {"code": "INVALID_MEDIA_FILE"}})
            body = req.content
            if i in self.corrupt_once:                      # a bit flipped in transit
                self.corrupt_once.discard(i)
                body = bytes([body[0] ^ 1]) + body[1:]
            if req.headers.get("content-md5") != _md5(body):
                return _err("UPLOAD_CHECKSUM_MISMATCH", 400, "The data was corrupted on the way.", chunk=i)
            self.received[i] = body
            return ok(self._video()["upload"])
        if req.method == "GET" and path.endswith("/upload"):
            return ok(self._video()["upload"])
        if req.method == "POST" and path.endswith("/upload/complete"):
            self.completes += 1
            if self.sha_mismatch_at_complete:
                self.received.clear()
                return _err("UPLOAD_CHECKSUM_MISMATCH", 400)
            if self.bad_at_complete:
                bad = sorted(self.bad_at_complete)
                self.bad_at_complete.clear()
                for i in bad:
                    self.received.pop(i, None)
                return _err("UPLOAD_INCOMPLETE", 409, missing_chunks=bad, reason="verification_failed")
            self.completed = True
            return ok(self._video("draft"))
        return httpx.Response(404, json={"success": False, "message": "no", "error": {"code": "NOT_FOUND"}})

    def assembled(self):
        return b"".join(self.received[i] for i in sorted(self.received))


def client_for(server):
    store = MemoryTokenStore()
    store.set_tokens("a" * 30, "r" * 30, 900)
    return ApiClient(store, http=httpx.Client(base_url="http://t", transport=httpx.MockTransport(server)), base_url="http://t")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(uploader, "_sleep", lambda attempt: None)


def source():
    return uploader.Source("clip.mp4", len(FILE), io.BytesIO(FILE))


def test_full_upload_with_progress():
    server, seen = FakeServer(), []
    video = uploader.start_and_upload(client_for(server), source(), title="Clip", description=None,
                                      category="Recipes", sort_order=0, on_progress=lambda *a: seen.append(a))
    assert video.status == "draft" and server.completed
    assert server.assembled() == FILE
    assert server.video["file_size"] == len(FILE) and server.video["content_type"] == "video/mp4"
    assert seen[0][2] == 0 and seen[-1][2] == 6 and seen[-1][0] == len(FILE)


def test_transient_failure_is_retried():
    server = FakeServer(fail_once={2})
    uploader.start_and_upload(client_for(server), source(), title="Clip", description=None, category=None, sort_order=0)
    assert server.puts.count(2) == 2 and server.assembled() == FILE


def test_client_error_stops_with_video_id():
    server = FakeServer(reject_chunk=0)
    with pytest.raises(uploader.UploadError) as info:
        uploader.start_and_upload(client_for(server), source(), title="Clip", description=None, category=None, sort_order=0)
    assert info.value.video_id == "v1" and "not a valid MP4" in info.value.message
    assert not server.completed


def test_resume_sends_only_missing():
    server = FakeServer()
    client = client_for(server)
    server.video = {"title": "Clip", "file_size": len(FILE)}
    for i in (0, 1, 4):
        server.received[i] = FILE[i * CHUNK:(i + 1) * CHUNK]
    from dashboard.models import Video
    video = Video.model_validate(server._video())
    uploader.resume(client, video, source())
    assert sorted(server.puts) == [2, 3, 5] and server.assembled() == FILE and server.completed


def test_resume_refuses_a_different_file():
    server = FakeServer()
    server.video = {"title": "Clip", "file_size": len(FILE) + 1}
    from dashboard.models import Video
    with pytest.raises(uploader.UploadError, match="same file"):
        uploader.resume(client_for(server), Video.model_validate(server._video()), source())


def test_source_from_path(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(FILE)
    s = uploader.source_from_path(f" '{f}' ")
    assert s.size == len(FILE) and s.name == "a.mp4" and s.read_chunk(1, CHUNK) == FILE[CHUNK:2 * CHUNK]
    with pytest.raises(uploader.UploadError, match="No file"):
        uploader.source_from_path(str(tmp_path / "missing.mp4"))
    (tmp_path / "a.mov").write_bytes(b"x")
    with pytest.raises(uploader.UploadError, match=".mp4"):
        uploader.source_from_path(str(tmp_path / "a.mov"))


def test_clean_path_variants(tmp_path):
    f = tmp_path / "My Clip.mp4"
    f.write_bytes(FILE)
    for raw in (str(f), f"  '{f}' ", f"file://{str(f).replace(' ', '%20')}", str(f).replace(" ", "\\ ")):
        assert uploader.source_from_path(raw).size == len(FILE), raw


def test_unicode_and_spacing_differences_are_tolerated(tmp_path):
    import unicodedata
    name = unicodedata.normalize("NFD", "Café  Recipe.mp4")   # macOS-style decomposed + double space
    (tmp_path / name).write_bytes(FILE)
    s = uploader.source_from_path(str(tmp_path / "Café Recipe.mp4"))
    assert s.size == len(FILE)


def test_videos_in_folder_newest_first(tmp_path):
    import os, time
    (tmp_path / "old.mp4").write_bytes(FILE)
    os.utime(tmp_path / "old.mp4", (time.time() - 100, time.time() - 100))
    (tmp_path / "new.m4v").write_bytes(FILE)
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".hidden.mp4").write_bytes(FILE)
    assert [v.name for v in uploader.videos_in_folder(str(tmp_path))] == ["new.m4v", "old.mp4"]
    with pytest.raises(uploader.UploadError, match="Folder not found"):
        uploader.videos_in_folder(str(tmp_path / "nope"))


def test_permission_denied_explains_macos(tmp_path, monkeypatch):
    import os
    def deny(*a, **k):
        raise PermissionError("Operation not permitted")
    monkeypatch.setattr(os, "scandir", deny)
    with pytest.raises(uploader.UploadError, match="Privacy & Security"):
        uploader.videos_in_folder(str(tmp_path))
    f = tmp_path / "a.mp4"
    f.write_bytes(FILE)
    monkeypatch.setattr(os, "stat", deny)
    with pytest.raises(uploader.UploadError, match="Privacy & Security"):
        uploader.source_from_path(str(f))


# ── integrity ────────────────────────────────────────────────────────────────

import hashlib

SHA = hashlib.sha256(FILE).hexdigest()


def _start(server, **kw):
    return uploader.start_and_upload(client_for(server), source(), title="Clip", description=None,
                                     category=None, sort_order=0, **kw)


def test_sends_fingerprint_and_md5_per_chunk():
    server, checks = FakeServer(), []
    _start(server, on_check=lambda done, total: checks.append((done, total)))
    assert server.video["sha256"] == SHA and server.video["allow_duplicate"] is False
    assert checks[-1] == (len(FILE), len(FILE)) and server.assembled() == FILE


def test_chunk_damaged_in_transit_is_sent_again():
    server = FakeServer(corrupt_once={1, 3})
    _start(server)
    assert server.puts.count(1) == 2 and server.puts.count(3) == 2
    assert server.assembled() == FILE and server.completed


def test_final_verification_failure_resends_only_those_chunks():
    server = FakeServer(bad_at_complete={2, 5})
    video = _start(server)
    assert video.status == "draft" and server.completes == 2
    assert server.puts.count(2) == 2 and server.puts.count(5) == 2 and server.puts.count(0) == 1
    assert server.assembled() == FILE


def test_whole_file_mismatch_stops_with_clear_message():
    server = FakeServer(sha_mismatch_at_complete=True)
    with pytest.raises(uploader.UploadError, match="changed while uploading") as info:
        _start(server)
    assert info.value.video_id == "v1" and server.completes == 1


def test_duplicate_is_reported_then_can_be_forced():
    existing = {"video_id": "old1", "title": "Lesson 1", "status": "published",
                "created_at": "2026-09-01T10:00:00Z", "uploaded_fraction": 1.0}
    server = FakeServer(known={SHA: existing})
    with pytest.raises(uploader.DuplicateFound) as info:
        _start(server)
    assert info.value.existing["title"] == "Lesson 1" and info.value.sha256 == SHA
    assert server.puts == []                                      # nothing sent
    video = _start(server, sha256=info.value.sha256, allow_duplicate=True)
    assert video.status == "draft" and server.assembled() == FILE


def test_resume_refuses_same_size_different_content():
    from dashboard.models import Video
    server = FakeServer()
    server.video = {"title": "Clip", "file_size": len(FILE)}
    video = Video.model_validate({**server._video(), "sha256": "0" * 64})
    with pytest.raises(uploader.UploadError, match="not the same content"):
        uploader.resume(client_for(server), video, source())
    video = Video.model_validate({**server._video(), "sha256": SHA})
    uploader.resume(client_for(server), video, source())
    assert server.completed


def test_fingerprint_is_cached_per_file(tmp_path, monkeypatch):
    f = tmp_path / "a.mp4"
    f.write_bytes(FILE)
    calls = []
    real = hashlib.sha256
    monkeypatch.setattr(uploader.hashlib, "sha256", lambda *a: calls.append(1) or real(*a))
    assert uploader.file_sha256(uploader.source_from_path(str(f))) == SHA
    assert uploader.file_sha256(uploader.source_from_path(str(f))) == SHA
    assert len(calls) == 1
    import os, time
    f.write_bytes(FILE[::-1])                                      # edited -> new fingerprint
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert uploader.file_sha256(uploader.source_from_path(str(f))) == hashlib.sha256(FILE[::-1]).hexdigest()
