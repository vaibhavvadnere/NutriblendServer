"""Browser uploads straight into R2: upload tickets, signed piece links,
reconciling progress with what R2 holds, and the HTTP routes."""

import base64
import hashlib
import os

import httpx
import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio
MB = 1024 * 1024


def _file(size=16 * MB) -> bytes:
    data = os.urandom(size)
    return data[:4] + b"ftyp" + data[8:]


def _md5(b: bytes) -> str:
    return base64.b64encode(hashlib.md5(b).digest()).decode()


async def _start(data: bytes):
    from app.services import video_service

    return await video_service.start_upload(
        {"_id": ObjectId()}, title="Clip", description=None, category=None, sort_order=0,
        file_name="clip.mp4", file_size=len(data), content_type="video/mp4", base_url="http://t",
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _chunks(data: bytes, size=5 * MB):
    return [data[i:i + size] for i in range(0, len(data), size)]


async def test_direct_parts_end_to_end(storage, mongo):
    from app.services import video_service

    data = _file()
    v = await _start(data)
    chunks = _chunks(data)                                  # 4 chunks of 5 MB (last 1 MB)
    links = await video_service.part_urls(v.id, [(i, _md5(c)) for i, c in enumerate(chunks)])
    assert links.parts[0].url is None                       # chunk 0 always via the server (MP4 check)
    assert all(p.url and p.headers == {"Content-MD5": _md5(chunks[p.index])} for p in links.parts[1:])
    assert "content-md5" in links.parts[1].url.lower()      # MD5 is part of the signature

    async with httpx.AsyncClient() as http:                  # the "browser"
        for p in links.parts[1:]:
            r = await http.put(p.url, content=chunks[p.index], headers=p.headers)
            assert r.status_code == 200, r.text
    progress = await video_service.upload_progress(v.id)
    assert progress.missing_chunks == [0]                   # 1..3 found in R2 and matched
    await video_service.write_chunk(await video_service.get_upload_doc(v.id), 0, chunks[0], _md5(chunks[0]))
    done = await video_service.complete_upload(v.id, "http://t")
    assert done.status == "draft" and done.integrity_verified

    url = await storage.presigned_url((await mongo["videos"].find_one({}))["file"]["key"], 60)
    assert hashlib.sha256(httpx.get(url).content).hexdigest() == done.sha256


async def test_part_with_other_bytes_is_not_counted(storage, mongo):
    """moto doesn't enforce Content-MD5 like R2 does; even so, a part whose
    stored MD5 differs from the announced one is never counted as received."""
    from app.services import video_service

    data = _file(11 * MB)
    v = await _start(data)
    chunks = _chunks(data)
    links = await video_service.part_urls(v.id, [(i, _md5(c)) for i, c in enumerate(chunks)])
    async with httpx.AsyncClient() as http:
        await http.put(links.parts[1].url, content=b"x" * len(chunks[1]), headers=links.parts[1].headers)
        await http.put(links.parts[2].url, content=chunks[2], headers=links.parts[2].headers)
    assert (await video_service.upload_progress(v.id)).missing_chunks == [0, 1]


async def test_local_storage_sends_everything_through_server(mongo, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.providers import storage as storage_pkg
    from app.providers.storage.local import LocalStorage
    from app.services import video_service

    monkeypatch.setattr(settings, "STORAGE_PROVIDER", "local")
    storage_pkg._cached = LocalStorage(str(tmp_path / "media"))
    try:
        data = _file(11 * MB)
        v = await _start(data)
        links = await video_service.part_urls(v.id, [(i, _md5(c)) for i, c in enumerate(_chunks(data, 8 * MB))])
        assert [p.url for p in links.parts] == [None, None] and links.expires_at is None
    finally:
        storage_pkg._cached = None


async def test_direct_upload_can_be_switched_off(storage, mongo, monkeypatch):
    from app.core.config import settings
    from app.services import video_service

    monkeypatch.setattr(settings, "UPLOAD_DIRECT_TO_STORAGE", False)
    data = _file(11 * MB)
    v = await _start(data)
    links = await video_service.part_urls(v.id, [(1, _md5(_chunks(data)[1]))])
    assert links.parts[0].url is None


# ── HTTP: ticket instead of the admin session ────────────────────────────────

async def test_upload_ticket_routes(storage, mongo):
    from app.core.security import create_upload_ticket
    from app.main import app

    data = _file(11 * MB)
    v = await _start(data)
    other = await _start(_file(6 * MB))
    ticket, _ = create_upload_ticket(v.id, "admin1")
    base = f"/api/v1/admin/videos/{v.id}/upload"
    chunks = _chunks(data)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
        assert (await api.get(base)).status_code == 401                           # nothing
        assert (await api.get(base, headers={"X-Upload-Ticket": "junk"})).status_code == 401
        wrong = create_upload_ticket(other.id, "admin1")[0]
        assert (await api.get(base, headers={"X-Upload-Ticket": wrong})).status_code == 401

        h = {"X-Upload-Ticket": ticket}
        r = await api.get(base, headers=h)
        assert r.status_code == 200 and r.json()["data"]["total_chunks"] == 3
        r = await api.post(f"{base}/part-urls", headers=h,
                           json={"parts": [{"index": i, "md5": _md5(c)} for i, c in enumerate(chunks)]})
        assert r.status_code == 200, r.text
        parts = r.json()["data"]["parts"]
        async with httpx.AsyncClient() as browser:
            for p in parts[1:]:
                assert (await browser.put(p["url"], content=chunks[p["index"]], headers=p["headers"])).status_code == 200
        r = await api.put(f"{base}/chunks/0", content=chunks[0],
                          headers={**h, "Content-Type": "application/octet-stream", "Content-MD5": _md5(chunks[0])})
        assert r.status_code == 200, r.text
        r = await api.post(f"{base}/complete", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "draft" and r.json()["data"]["integrity_verified"]

        # A ticket is not a session: other admin endpoints refuse it.
        assert (await api.get(f"/api/v1/admin/videos/{v.id}", headers={"Authorization": f"Bearer {ticket}"})).status_code == 401
        # Ticket for a finished upload: nothing left to do.
        assert (await api.post(f"{base}/part-urls", headers=h,
                               json={"parts": [{"index": 1, "md5": _md5(chunks[1])}]})).status_code == 409


async def test_part_url_request_validation(storage, mongo):
    from app.core.security import create_upload_ticket
    from app.main import app

    v = await _start(_file(6 * MB))
    h = {"X-Upload-Ticket": create_upload_ticket(v.id, "a")[0]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
        url = f"/api/v1/admin/videos/{v.id}/upload/part-urls"
        assert (await api.post(url, headers=h, json={"parts": [{"index": 1, "md5": "nope"}]})).status_code == 422
        assert (await api.post(url, headers=h, json={"parts": [{"index": 9, "md5": _md5(b"x")}]})).status_code == 400
        assert (await api.post(url, headers=h, json={"parts": []})).status_code == 422


async def test_cors_allows_dashboard_origin():
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
        r = await api.options("/api/v1/admin/videos/x/upload/part-urls", headers={
            "Origin": "http://localhost:8501", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-upload-ticket,content-type"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] in ("http://localhost:8501", "*")
