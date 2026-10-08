"""Upload integrity: per-chunk MD5 (client → server → storage), verification of
what the storage holds before completing, whole-file SHA-256, and duplicate
detection. Runs against R2 (moto) and local storage."""

import base64
import hashlib
import os

import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio
MB = 1024 * 1024


def _file(size=11 * MB) -> bytes:
    data = os.urandom(size)
    return data[:4] + b"ftyp" + data[8:]


def _md5(b: bytes) -> str:
    return base64.b64encode(hashlib.md5(b).digest()).decode()


async def _start(data: bytes, *, sha=True, allow_duplicate=False, title="Clip"):
    from app.services import video_service

    return await video_service.start_upload(
        {"_id": ObjectId()}, title=title, description=None, category=None, sort_order=0,
        file_name="clip.mp4", file_size=len(data), content_type="video/mp4", base_url="http://t",
        sha256=hashlib.sha256(data).hexdigest() if sha else None, allow_duplicate=allow_duplicate,
    )


async def _send_all(video_id: str, data: bytes, *, with_md5=True, only=None):
    from app.services import video_service

    doc = await video_service.get_upload_doc(video_id)
    size = doc["upload"]["chunk_size"]
    for i in range(doc["upload"]["total_chunks"]):
        if only is not None and i not in only:
            continue
        chunk = data[i * size:(i + 1) * size]
        doc = await video_service.get_upload_doc(video_id)
        await video_service.write_chunk(doc, i, chunk, _md5(chunk) if with_md5 else None)


@pytest.fixture()
def local_storage(mongo, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.providers import storage as storage_pkg
    from app.providers.storage.local import LocalStorage

    monkeypatch.setattr(settings, "STORAGE_PROVIDER", "local")
    storage_pkg._cached = LocalStorage(str(tmp_path / "media"))
    yield storage_pkg._cached
    storage_pkg._cached = None


# ── R2 ───────────────────────────────────────────────────────────────────────

async def test_r2_verified_upload_end_to_end(storage, mongo):
    from app.services import video_service

    data = _file()
    v = await _start(data)
    await _send_all(v.id, data)
    done = await video_service.complete_upload(v.id, "http://t")
    assert done.status == "draft" and done.integrity_verified and done.sha256 == hashlib.sha256(data).hexdigest()
    url = await storage.presigned_url((await mongo["videos"].find_one({}))["file"]["key"], 60)
    import httpx
    assert hashlib.sha256(httpx.get(url).content).hexdigest() == done.sha256


async def test_corrupted_chunk_from_client_is_rejected(storage, mongo):
    from app.core.exceptions import UploadChecksumMismatch
    from app.services import video_service

    data = _file()
    v = await _start(data)
    doc = await video_service.get_upload_doc(v.id)
    chunk = data[:5 * MB]
    corrupted = bytes([chunk[0] ^ 1]) + chunk[1:]
    with pytest.raises(UploadChecksumMismatch):
        await video_service.write_chunk(doc, 0, corrupted, _md5(chunk))
    progress = await video_service.upload_progress(v.id)
    assert progress.received_chunks == 0          # nothing recorded
    await video_service.write_chunk(await video_service.get_upload_doc(v.id), 0, chunk, _md5(chunk))
    assert (await video_service.upload_progress(v.id)).received_chunks == 1


async def test_r2_itself_rejects_bytes_that_dont_match_md5(storage, monkeypatch):
    """moto ignores Content-MD5, so emulate R2: check it and answer BadDigest."""
    import uuid

    from botocore.exceptions import ClientError

    from app.providers.storage.base import ChecksumMismatch

    real, sent = storage.client.upload_part, []

    def r2_like_upload_part(**kw):
        sent.append(kw.get("ContentMD5"))
        if kw.get("ContentMD5") and kw["ContentMD5"] != _md5(kw["Body"]):
            raise ClientError({"Error": {"Code": "BadDigest", "Message": "md5 mismatch"}}, "UploadPart")
        return real(**kw)

    monkeypatch.setattr(storage.client, "upload_part", r2_like_upload_part)
    key = f"videos/{ObjectId()}/{uuid.uuid4().hex}.mp4"
    data = _file(6 * MB)
    await storage.begin_upload(key, len(data))
    with pytest.raises(ChecksumMismatch):
        await storage.write_chunk(key, 0, data[:5 * MB], content_md5=_md5(b"something else"))
    await storage.write_chunk(key, 0, data[:5 * MB], content_md5=_md5(data[:5 * MB]))
    assert sent == [_md5(b"something else"), _md5(data[:5 * MB])]   # header really goes to R2


async def test_complete_detects_part_that_differs_and_asks_for_it_again(storage, mongo):
    from app.core.exceptions import UploadIncomplete
    from app.services import video_service

    data = _file()
    v = await _start(data)
    await _send_all(v.id, data)
    # Pretend chunk 1 arrived with different content than what R2 now holds.
    await mongo["videos"].update_one({}, {"$set": {"upload.md5.1": "0" * 32}})
    with pytest.raises(UploadIncomplete) as info:
        await video_service.complete_upload(v.id, "http://t")
    assert info.value.details["missing_chunks"] == [1] and info.value.details["reason"] == "verification_failed"
    progress = await video_service.upload_progress(v.id)
    assert progress.missing_chunks == [1]          # forgotten -> client re-sends it
    await _send_all(v.id, data, only={1})
    done = await video_service.complete_upload(v.id, "http://t")
    assert done.status == "draft" and done.integrity_verified


async def test_old_clients_without_md5_still_work(storage, mongo):
    from app.services import video_service

    data = _file()
    v = await _start(data, sha=False)
    await _send_all(v.id, data, with_md5=False)
    done = await video_service.complete_upload(v.id, "http://t")
    assert done.status == "draft" and done.sha256 is None
    assert done.integrity_verified   # server-computed MD5s matched what R2 holds


# ── duplicates ───────────────────────────────────────────────────────────────

async def test_duplicate_file_is_detected(storage, mongo):
    from app.core.exceptions import DuplicateVideo
    from app.services import video_service

    data = _file(6 * MB)
    first = await _start(data, title="Original")
    # While the first is still uploading: point to it so it can be resumed.
    with pytest.raises(DuplicateVideo) as info:
        await _start(data, title="Again")
    d = info.value.details
    assert d["video_id"] == first.id and d["status"] == "uploading" and d["uploaded_fraction"] == 0.0
    assert "resume" in info.value.message

    await _send_all(first.id, data)
    await video_service.complete_upload(first.id, "http://t")
    with pytest.raises(DuplicateVideo) as info:
        await _start(data, title="Again")
    assert info.value.details["status"] == "draft" and info.value.details["title"] == "Original"

    second = await _start(data, title="Deliberate copy", allow_duplicate=True)
    assert second.status == "uploading"
    other = await _start(_file(6 * MB), title="Different file")
    assert other.status == "uploading"


# ── local storage ────────────────────────────────────────────────────────────

async def test_local_whole_file_sha256_and_part_rereading(local_storage, mongo):
    from app.core.exceptions import UploadChecksumMismatch, UploadIncomplete
    from app.services import video_service

    data = _file()
    v = await _start(data)
    await _send_all(v.id, data)
    # Flip a byte on disk inside chunk 2 (e.g. disk fault): re-reading catches it.
    key = (await mongo["videos"].find_one({}))["file"]["key"]
    part = local_storage._part(key)
    with open(part, "r+b") as fh:
        fh.seek(10 * MB + 5)
        b = fh.read(1)
        fh.seek(10 * MB + 5)
        fh.write(bytes([b[0] ^ 0xFF]))
    with pytest.raises(UploadIncomplete) as info:
        await video_service.complete_upload(v.id, "http://t")
    assert info.value.details["missing_chunks"] == [2]
    await _send_all(v.id, data, only={2})
    done = await video_service.complete_upload(v.id, "http://t")
    assert done.integrity_verified

    # Declared SHA-256 of a different file -> reset and refuse.
    other = _file()
    v2 = await video_service.start_upload(
        {"_id": ObjectId()}, title="Wrong sha", description=None, category=None, sort_order=0,
        file_name="b.mp4", file_size=len(other), content_type="video/mp4", base_url="http://t",
        sha256=hashlib.sha256(b"not this file").hexdigest(),
    )
    await _send_all(v2.id, other)
    with pytest.raises(UploadChecksumMismatch):
        await video_service.complete_upload(v2.id, "http://t")
    assert (await video_service.upload_progress(v2.id)).received_chunks == 0
