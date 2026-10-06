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


class FakeServer:
    def __init__(self, fail_once=(), reject_chunk=None):
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
            return ok(self._video(), 201)
        if req.method == "PUT" and "/upload/chunks/" in path:
            i = int(path.rsplit("/", 1)[1])
            self.puts.append(i)
            if i in self.fail_once:
                self.fail_once.discard(i)
                return httpx.Response(502, json={"success": False, "message": "bad gateway", "error": {"code": "INTERNAL_ERROR"}})
            if i == self.reject_chunk:
                return httpx.Response(422, json={"success": False, "message": "This file is not a valid MP4 video.", "error": {"code": "INVALID_MEDIA_FILE"}})
            self.received[i] = req.content
            return ok(self._video()["upload"])
        if req.method == "GET" and path.endswith("/upload"):
            return ok(self._video()["upload"])
        if req.method == "POST" and path.endswith("/upload/complete"):
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
