"""Storage check: records whose files are missing from R2 are found, can't be
published, and can be deleted (against moto + mongomock)."""

import uuid
from datetime import datetime, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio


async def _record(mongo, storage, title, *, upload_video=True, status="draft", with_doc=False, with_thumb=False):
    """Insert a video record directly; optionally put its files in the bucket."""
    vid = ObjectId()
    key = f"videos/{vid}/{uuid.uuid4().hex}.mp4"
    doc = {
        "_id": vid, "title": title, "description": None, "category": None, "sort_order": 0, "status": status,
        "file": {"key": key, "size": 100, "content_type": "video/mp4", "original_name": "a.mp4"},
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }
    if upload_video:
        await storage.put(key, b"\x00\x00\x00\x18ftypmp42" + b"x" * 88, "video/mp4")
    if with_thumb:
        doc["thumbnail"] = {"key": f"thumbnails/{vid}/{uuid.uuid4().hex}.jpg", "size": 3, "content_type": "image/jpeg"}
    if with_doc:
        d_id = uuid.uuid4().hex
        doc["document"] = {"id": d_id, "key": f"documents/{vid}/{d_id}.pdf", "name": "a.pdf", "file_type": "pdf",
                           "content_type": "application/pdf", "size": 10, "status": "ready",
                           "uploaded_at": datetime.now(timezone.utc), "pdf_key": f"documents/{vid}/{d_id}.pdf",
                           "page_count": 1}
    await mongo["videos"].insert_one(doc)
    return str(vid)


async def test_storage_check_finds_only_records_with_missing_files(storage, mongo):
    from app.services import video_service

    good = await _record(mongo, storage, "In R2")
    old = await _record(mongo, storage, "Uploaded before R2", upload_video=False, status="published",
                        with_doc=True, with_thumb=True)
    await _record(mongo, storage, "Still uploading", upload_video=False, status="uploading")

    result = await video_service.storage_check()
    assert result.storage == "r2" and result.checked == 3 and result.errors == 0
    assert [(b.id, b.missing_files) for b in result.broken] == [(old, ["video", "thumbnail", "document"])]

    out = await video_service.get_admin_checked(good, "http://t")
    assert out.missing_files == []
    out = await video_service.get_admin_checked(old, "http://t")
    assert out.missing_files == ["video", "thumbnail", "document"]


async def test_cannot_publish_video_whose_file_is_missing(storage, mongo):
    from app.core.exceptions import VideoNotReady
    from app.services import video_service

    broken = await _record(mongo, storage, "Gone", upload_video=False)
    with pytest.raises(VideoNotReady, match="missing from storage"):
        await video_service.set_status(broken, "published", "http://t")

    fine = await _record(mongo, storage, "Fine")
    assert (await video_service.set_status(fine, "published", "http://t")).status == "published"


async def test_delete_works_when_files_are_already_gone(storage, mongo):
    from app.services import video_service

    broken = await _record(mongo, storage, "Gone", upload_video=False, with_doc=True, with_thumb=True)
    await video_service.delete_video(broken)
    assert await mongo["videos"].count_documents({}) == 0
    assert (await video_service.storage_check()).broken == []


async def test_unreachable_storage_is_reported_not_guessed(storage, mongo, monkeypatch):
    from app.providers.storage import StorageError
    from app.services import video_service

    await _record(mongo, storage, "Any")

    async def boom(key):
        raise StorageError("R2: connection refused")

    monkeypatch.setattr(storage, "exists", boom)
    result = await video_service.storage_check()
    assert result.broken == [] and result.errors == 1
