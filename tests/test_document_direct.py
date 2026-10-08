"""Documents sent from the browser straight into R2: signed PUT link, finish
(size + type check), SHA-256 check while preparing, tickets and routes."""

import hashlib
import io
from pathlib import Path

import httpx
import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio


def _pdf(pages=2) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    imgs = [Image.new("RGB", (600, 800), (255, 255 - 60 * i, 255)) for i in range(pages)]
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


async def _video(mongo) -> str:
    vid = ObjectId()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    await mongo["videos"].insert_one({"_id": vid, "title": "t", "status": "draft", "sort_order": 0,
                                      "created_at": now, "updated_at": now,
                                      "file": {"key": f"videos/{vid}/a.mp4", "size": 1, "content_type": "video/mp4"}})
    return str(vid)


async def _browser_put(link, data: bytes) -> int:
    async with httpx.AsyncClient() as browser:
        return (await browser.put(link.url, content=data, headers=link.headers)).status_code


async def test_direct_pdf_end_to_end(storage, mongo):
    from app.services import document_service

    vid = await _video(mongo)
    data = _pdf(3)
    link = await document_service.start_direct(vid, "Recipe%20Sheet.pdf", len(data), hashlib.sha256(data).hexdigest())
    assert link.url and link.headers == {"Content-Type": "application/pdf"} and "content-length" in link.url.lower()
    assert await _browser_put(link, data) == 200
    video = await document_service.finish_direct(vid, link.upload_id)
    d = video["document"]
    assert d["name"] == "Recipe Sheet.pdf" and d["status"] == "processing" and "document_upload" not in video
    await document_service.prepare(vid)
    d = (await mongo["videos"].find_one({"_id": ObjectId(vid)}))["document"]
    assert d["status"] == "ready" and d["page_count"] == 3
    # finishing twice (a retried request) is harmless
    assert (await document_service.finish_direct(vid, link.upload_id))["document"]["id"] == d["id"]


async def test_incomplete_or_missing_file_is_refused(storage, mongo):
    from app.core.exceptions import UploadIncomplete
    from app.services import document_service

    vid = await _video(mongo)
    data = _pdf()
    link = await document_service.start_direct(vid, "a.pdf", len(data), None)
    with pytest.raises(UploadIncomplete, match="hasn't arrived"):
        await document_service.finish_direct(vid, link.upload_id)
    # R2 would refuse a body whose length differs from the signed one; simulate a
    # short object anyway (e.g. written another way) -> refused and removed.
    await storage.put(f"documents/{vid}/{link.upload_id}.pdf", data[:100], "application/pdf")
    with pytest.raises(UploadIncomplete, match="Only 100"):
        await document_service.finish_direct(vid, link.upload_id)
    assert not await storage.exists(f"documents/{vid}/{link.upload_id}.pdf")


async def test_wrong_kind_of_file_is_refused(storage, mongo):
    from app.core.exceptions import InvalidMediaFile
    from app.services import document_service

    vid = await _video(mongo)
    fake = b"this is not a pdf at all" * 10
    link = await document_service.start_direct(vid, "a.pdf", len(fake), None)
    await _browser_put(link, fake)
    with pytest.raises(InvalidMediaFile):
        await document_service.finish_direct(vid, link.upload_id)
    video = await mongo["videos"].find_one({"_id": ObjectId(vid)})
    assert "document" not in video and "document_upload" not in video


async def test_damaged_bytes_fail_sha256_check(storage, mongo):
    from app.services import document_service

    vid = await _video(mongo)
    data = _pdf()
    link = await document_service.start_direct(vid, "a.pdf", len(data), hashlib.sha256(data).hexdigest())
    damaged = data[:-20] + bytes(20)                     # same size, different bytes
    await _browser_put(link, damaged)
    await document_service.finish_direct(vid, link.upload_id)
    await document_service.prepare(vid)
    d = (await mongo["videos"].find_one({"_id": ObjectId(vid)}))["document"]
    assert d["status"] == "failed" and "damaged during upload" in d["error"]


async def test_replacing_keeps_old_until_new_is_finished(storage, mongo):
    from app.services import document_service

    vid = await _video(mongo)
    first = _pdf(1)
    link = await document_service.start_direct(vid, "one.pdf", len(first), None)
    await _browser_put(link, first)
    await document_service.finish_direct(vid, link.upload_id)
    old_key = f"documents/{vid}/{link.upload_id}.pdf"

    second = _pdf(2)
    link2 = await document_service.start_direct(vid, "two.pdf", len(second), None)
    video = await mongo["videos"].find_one({"_id": ObjectId(vid)})
    assert video["document"]["name"] == "one.pdf" and await storage.exists(old_key)   # still there
    # started again without finishing: the abandoned object is removed
    await _browser_put(link2, second)
    link3 = await document_service.start_direct(vid, "two.pdf", len(second), None)
    assert not await storage.exists(f"documents/{vid}/{link2.upload_id}.pdf")
    await _browser_put(link3, second)
    await document_service.finish_direct(vid, link3.upload_id)
    assert not await storage.exists(old_key)                                          # old one gone now
    assert (await mongo["videos"].find_one({"_id": ObjectId(vid)}))["document"]["name"] == "two.pdf"


async def test_local_storage_goes_through_server_with_sha_check(mongo, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.core.exceptions import UploadChecksumMismatch
    from app.providers import storage as storage_pkg
    from app.providers.storage.local import LocalStorage
    from app.services import document_service

    monkeypatch.setattr(settings, "STORAGE_PROVIDER", "local")
    storage_pkg._cached = LocalStorage(str(tmp_path / "media"))
    try:
        vid = await _video(mongo)
        data = _pdf()
        link = await document_service.start_direct(vid, "a.pdf", len(data), None)
        assert link.url is None

        async def body():
            yield data

        with pytest.raises(UploadChecksumMismatch):
            await document_service.upload(vid, "a.pdf", body(), len(data), sha256="0" * 64)
        video, _ = await document_service.upload(vid, "a.pdf", body(), len(data), sha256=hashlib.sha256(data).hexdigest())
        assert video["document"]["sha256"] == hashlib.sha256(data).hexdigest()
    finally:
        storage_pkg._cached = None


async def test_document_ticket_routes(storage, mongo):
    from app.core.security import UPLOAD_FOR_DOCUMENT, create_upload_ticket
    from app.main import app

    vid = await _video(mongo)
    other = await _video(mongo)
    data = _pdf()
    doc_ticket = create_upload_ticket(vid, "admin1", UPLOAD_FOR_DOCUMENT)[0]
    video_ticket = create_upload_ticket(vid, "admin1")[0]
    base = f"/api/v1/admin/videos/{vid}/document"
    body = {"file_name": "Notes.pdf", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
        assert (await api.post(f"{base}/upload", json=body)).status_code == 401
        # a video-upload ticket can't upload documents, nor a ticket for another video
        assert (await api.post(f"{base}/upload", json=body, headers={"X-Upload-Ticket": video_ticket})).status_code == 401
        wrong = create_upload_ticket(other, "a", UPLOAD_FOR_DOCUMENT)[0]
        assert (await api.post(f"{base}/upload", json=body, headers={"X-Upload-Ticket": wrong})).status_code == 401
        # and a document ticket can't send video pieces
        assert (await api.get(f"/api/v1/admin/videos/{vid}/upload", headers={"X-Upload-Ticket": doc_ticket})).status_code == 401

        h = {"X-Upload-Ticket": doc_ticket}
        r = await api.post(f"{base}/upload", json=body, headers=h)
        assert r.status_code == 200, r.text
        link = r.json()["data"]
        async with httpx.AsyncClient() as browser:
            assert (await browser.put(link["url"], content=data, headers=link["headers"])).status_code == 200
        r = await api.post(f"{base}/upload/{link['upload_id']}/complete", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["admin_document"]["name"] == "Notes.pdf"

        # through-the-server path with the same ticket
        r = await api.put(base, content=data, headers={**h, "X-File-Name": "Again.pdf",
                                                         "X-File-SHA256": hashlib.sha256(data).hexdigest()})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["admin_document"]["name"] == "Again.pdf"
        bad = await api.put(base, content=data, headers={**h, "X-File-Name": "x.pdf", "X-File-SHA256": "1" * 64})
        assert bad.status_code == 400

        assert (await api.post(f"{base}/upload", headers=h,
                               json={"file_name": "x.exe", "size": 5})).status_code == 415
