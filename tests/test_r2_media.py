"""R2 storage, chunked video upload, documents and /media redirects (against moto + mongomock)."""

import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio

MB = 1024 * 1024


def _key(kind="videos", ext="mp4"):
    return f"{kind}/{ObjectId()}/{uuid.uuid4().hex}.{ext}"


@pytest.fixture(scope="session")
def mp4(tmp_path_factory) -> bytes:
    """A real ~11 MB MP4 (noise compresses badly), so the upload needs 3 parts."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("v") / "noise.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "nullsrc=s=640x360:d=4,geq=random(1)*255:128:128",
         "-c:v", "libx264", "-b:v", "24M", "-maxrate", "24M", "-bufsize", "8M", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(out)],
        check=True,
    )
    data = out.read_bytes()
    assert len(data) > 10 * MB, len(data)
    return data


# ── storage provider ─────────────────────────────────────────────────────────

async def test_multipart_upload_any_order_and_resend(storage):
    key = _key()
    data = os.urandom(12 * MB)
    data = data[:4] + b"ftyp" + data[8:]
    part = 5 * MB
    await storage.begin_upload(key, len(data))
    offsets = [2 * part, 0, part, part]  # out of order, chunk 1 sent twice
    for off in offsets:
        await storage.write_chunk(key, off, data[off:off + part])
    assert (await storage.read_upload_head(key, 12))[4:8] == b"ftyp"
    await storage.complete_upload(key)
    assert await storage.exists(key)

    url = await storage.presigned_url(key, 60)
    r = httpx.get(url, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200]
    full = httpx.get(url)
    assert full.content == data
    assert full.headers.get("content-disposition") == "inline"

    await storage.delete(key)
    assert not await storage.exists(key)


async def test_complete_refuses_missing_part(storage):
    from app.providers.storage import StorageError

    key = _key()
    await storage.begin_upload(key, 11 * MB)
    await storage.write_chunk(key, 0, b"\0\0\0\0ftyp" + os.urandom(5 * MB - 8))
    with pytest.raises(StorageError):
        await storage.complete_upload(key)
    await storage.abort_upload(key)
    await storage.abort_upload(key)  # idempotent


async def test_invalid_keys_rejected(storage):
    from app.providers.storage import StorageError

    for bad in ["../etc/passwd", "videos/x/y.mp4", "documents/../../a"]:
        with pytest.raises(StorageError):
            await storage.put(bad, b"x", "text/plain")


async def test_put_file_download_and_prefix_delete(storage, tmp_path):
    vid, doc = ObjectId(), uuid.uuid4().hex
    for n in (1, 2, 3):
        src = tmp_path / f"p{n}.jpg"
        src.write_bytes(b"jpg%d" % n)
        await storage.put_file(f"pages/{vid}/{doc}/p{n:04d}.jpg", src, "image/jpeg")
        assert not src.exists()  # consumed, like local storage
    dest = tmp_path / "out" / "copy.jpg"
    await storage.download_to(f"pages/{vid}/{doc}/p0002.jpg", dest)
    assert dest.read_bytes() == b"jpg2"
    await storage.delete_prefix(f"pages/{vid}/{doc}")
    assert not await storage.exists(f"pages/{vid}/{doc}/p0001.jpg")


# ── video upload flow ────────────────────────────────────────────────────────

async def _upload_video(mp4: bytes, title="Test") -> str:
    from app.services import video_service

    admin = {"_id": ObjectId()}
    out = await video_service.start_upload(
        admin, title=title, description=None, category="test", sort_order=0,
        file_name="noise.mp4", file_size=len(mp4), content_type="video/mp4", base_url="http://api.test/",
    )
    size = out.upload.chunk_size
    for i in reversed(range(out.upload.total_chunks)):  # any order
        doc = await video_service.get_upload_doc(out.id)
        await video_service.write_chunk(doc, i, mp4[i * size:(i + 1) * size])
    done = await video_service.complete_upload(out.id, "http://api.test/")
    assert done.status == "draft"
    return out.id


async def test_video_upload_probe_and_playback_redirect(storage, mp4):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import video_service

    video_id = await _upload_video(mp4)
    admin_out = await video_service.get_admin(video_id, "http://testserver/")
    assert admin_out.duration_seconds and admin_out.duration_seconds > 3  # ffprobe read it over the presigned URL
    assert admin_out.video_codec == "h264"
    assert await storage._uploads().count_documents({}) == 0  # bookkeeping cleaned up

    client = TestClient(app)  # no `with`: lifespan (real Mongo connect) is not run
    path = admin_out.playback_url.replace("http://testserver", "")
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302 and r.headers["cache-control"] == "private, no-store"
    r2 = httpx.get(r.headers["location"], headers={"Range": "bytes=0-11"})
    assert r2.status_code == 206 and r2.content == mp4[:12]

    # tampered / expired links are refused before any redirect
    assert client.get(path.replace("sig=", "sig=x"), follow_redirects=False).status_code != 302

    await video_service.delete_video(video_id)
    key = (await storage._uploads().find_one({})) or None
    assert key is None


async def test_rejects_non_mp4_first_chunk(storage):
    from app.core.exceptions import InvalidMediaFile
    from app.services import video_service

    out = await video_service.start_upload(
        {"_id": ObjectId()}, title="x", description=None, category=None, sort_order=0,
        file_name="x.mp4", file_size=6 * MB, content_type="video/mp4", base_url="http://t/",
    )
    doc = await video_service.get_upload_doc(out.id)
    with pytest.raises(InvalidMediaFile):
        await video_service.write_chunk(doc, 0, b"\0" * (5 * MB))
    await video_service.delete_video(out.id)


async def test_expired_upload_cleanup_aborts_multipart(storage, mongo):
    from datetime import datetime, timedelta, timezone

    from app.services import video_service

    out = await video_service.start_upload(
        {"_id": ObjectId()}, title="x", description=None, category=None, sort_order=0,
        file_name="x.mp4", file_size=6 * MB, content_type="video/mp4", base_url="http://t/",
    )
    await mongo["videos"].update_one(
        {"_id": ObjectId(out.id)},
        {"$set": {"upload.expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}},
    )
    assert await video_service.cleanup_expired_uploads() == 1
    assert await storage._uploads().count_documents({}) == 0


# ── documents ────────────────────────────────────────────────────────────────

def _make_pdf(path: Path, pages=2):
    from PIL import Image

    imgs = [Image.new("RGB", (600, 800), (255, 255 - 60 * i, 255)) for i in range(pages)]
    imgs[0].save(path, "PDF", save_all=True, append_images=imgs[1:])


async def _stream(path: Path):
    yield path.read_bytes()


async def _video_with_doc(mongo, storage, tmp_path, src: Path, name: str):
    from app.services import document_service

    vid = ObjectId()
    await mongo["videos"].insert_one({"_id": vid, "title": "t", "status": "draft", "file": {"key": _key(), "size": 1, "content_type": "video/mp4"}})
    await document_service.upload(str(vid), name, _stream(src), src.stat().st_size)
    await document_service.prepare(str(vid))
    return vid


async def test_pdf_document_pages_render_into_bucket(storage, mongo, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app
    from app.services import document_service

    monkeypatch.setattr(settings, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=3)
    vid = await _video_with_doc(mongo, storage, tmp_path, src, "Notes.pdf")
    video = await mongo["videos"].find_one({"_id": vid})
    d = video["document"]
    assert d["status"] == "ready", d.get("error")
    assert d["page_count"] == 3 and d["pdf_key"] == d["key"]

    pages = document_service.pages_for(video, "http://testserver/")
    client = TestClient(app)
    r = client.get(pages.pages[1].replace("http://testserver", ""), follow_redirects=False)
    assert r.status_code == 302
    img = httpx.get(r.headers["location"])
    assert img.content[:3] == b"\xff\xd8\xff"  # JPEG
    assert await storage.exists(document_service.page_key(vid, d["id"], 2))
    assert len(list((tmp_path / "cache").glob("*.pdf"))) == 1  # PDF cached for the next pages

    # the original document is never served
    from app.core import media_links
    orig = media_links.signed_url("http://testserver/", d["key"]).replace("http://testserver", "")
    assert client.get(orig, follow_redirects=False).status_code == 404
    # a page that doesn't exist
    bad = media_links.signed_url("http://testserver/", document_service.page_key(vid, d["id"], 9)).replace("http://testserver", "")
    assert client.get(bad, follow_redirects=False).status_code == 404

    await document_service.remove(str(vid))
    assert not await storage.exists(d["key"])
    assert not await storage.exists(document_service.page_key(vid, d["id"], 2))


async def test_docx_converted_with_libreoffice(storage, mongo, tmp_path):
    if not shutil.which("soffice"):
        pytest.skip("LibreOffice not installed")
    import zipfile

    src = tmp_path / "report.docx"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Nutriblend test</w:t></w:r></w:p></w:body></w:document>')
    vid = await _video_with_doc(mongo, storage, tmp_path, src, "report.docx")
    d = (await mongo["videos"].find_one({"_id": vid}))["document"]
    assert d["status"] == "ready", d.get("error")
    assert d["pdf_key"].endswith(".pdf") and await storage.exists(d["pdf_key"])
    assert d["page_count"] == 1


async def test_resume_unfinished_documents(storage, mongo, tmp_path):
    from app.services import document_service

    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=1)
    vid = await _video_with_doc(mongo, storage, tmp_path, src, "a.pdf")
    await mongo["videos"].update_one({"_id": vid}, {"$set": {"document.status": "processing"}})
    assert await document_service.resume_unfinished() == 1
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await mongo["videos"].find_one({"_id": vid}))["document"]["status"] == "ready":
            break
    assert (await mongo["videos"].find_one({"_id": vid}))["document"]["status"] == "ready"


# ── local storage still works (development) ──────────────────────────────────

async def test_local_storage_flow_unchanged(mongo, tmp_path, monkeypatch, mp4):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.providers import storage as storage_pkg
    from app.providers.storage.local import LocalStorage
    from app.services import document_service, video_service

    monkeypatch.setattr(storage_pkg, "_cached", LocalStorage(str(tmp_path / "media")))
    video_id = await _upload_video(mp4)
    out = await video_service.get_admin(video_id, "http://testserver/")
    assert out.video_codec == "h264"
    client = TestClient(app)
    r = client.get(out.playback_url.replace("http://testserver", ""), headers={"Range": "bytes=0-11"})
    assert r.status_code == 206 and r.content == mp4[:12]

    src = tmp_path / "d.pdf"
    _make_pdf(src, pages=2)
    await document_service.upload(video_id, "d.pdf", _stream(src), src.stat().st_size)
    await document_service.prepare(video_id)
    video = await mongo["videos"].find_one({"_id": ObjectId(video_id)})
    assert video["document"]["status"] == "ready"
    page = document_service.pages_for(video, "http://testserver/").pages[0]
    r = client.get(page.replace("http://testserver", ""))
    assert r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff"
    await video_service.delete_video(video_id)
