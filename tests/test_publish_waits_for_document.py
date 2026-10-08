"""A video with a document is only shown in the app once the document is ready:
publishing waits (409, or "publish when ready"), and the app hides published videos
whose document isn't ready."""

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio


def _pdf(pages=2) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    imgs = [Image.new("RGB", (600, 800), (255, 255 - 60 * i, 255)) for i in range(pages)]
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


async def _video(storage, mongo, *, status="draft", doc=None, **extra) -> str:
    """A finished video (its file exists in storage), optionally with a document record in a given state."""
    vid = ObjectId()
    now = datetime.now(timezone.utc)
    key = f"videos/{vid}/{uuid.uuid4().hex}.mp4"
    await storage.put(key, b"\x00\x00\x00\x18ftypmp42" + b"v" * 100, "video/mp4")
    record = {"_id": vid, "title": "Lesson", "status": status, "sort_order": 0, "created_at": now, "updated_at": now,
              "file": {"key": key, "size": 100, "content_type": "video/mp4"}, **extra}
    if status == "published":
        record["published_at"] = now
    if doc:
        record["document"] = await _document(storage, vid, **doc)
    await mongo["videos"].insert_one(record)
    return str(vid)


async def _document(storage, vid, status="processing", data=None) -> dict:
    doc_id = uuid.uuid4().hex
    key = f"documents/{vid}/{doc_id}.pdf"
    await storage.put(key, data if data is not None else _pdf(), "application/pdf")
    return {"id": doc_id, "key": key, "name": "Recipe.pdf", "file_type": "pdf", "content_type": "application/pdf",
            "size": 1, "uploaded_at": datetime.now(timezone.utc), "sha256": None, "status": status, "error": None,
            "pdf_key": key, "page_count": 2 if status == "ready" else None}


async def _stored(mongo, vid) -> dict:
    return await mongo["videos"].find_one({"_id": ObjectId(vid)})


async def _app_ids() -> list[str]:
    from app.services import video_service

    return [v.id for v in (await video_service.list_published(None, 1, 50, "http://t")).items]


async def test_video_without_document_publishes_at_once(storage, mongo):
    from app.services import video_service

    vid = await _video(storage, mongo)
    out = await video_service.set_status(vid, "published", "http://t")
    assert out.status == "published" and out.visible_in_app and not out.publish_when_ready
    assert vid in await _app_ids()


async def test_publish_is_refused_while_the_document_is_preparing(storage, mongo):
    from app.core.exceptions import DocumentNotReady
    from app.services import video_service

    vid = await _video(storage, mongo, doc={"status": "processing"})
    with pytest.raises(DocumentNotReady) as e:
        await video_service.set_status(vid, "published", "http://t")
    assert e.value.details == {"document_status": "processing", "can_publish_when_ready": True}
    assert (await _stored(mongo, vid))["status"] == "draft"


async def test_publish_when_ready_goes_live_by_itself(storage, mongo):
    from app.services import document_service, video_service

    vid = await _video(storage, mongo, doc={"status": "processing"})
    out = await video_service.set_status(vid, "published", "http://t", when_ready=True)
    assert out.status == "draft" and out.publish_when_ready and not out.visible_in_app
    assert vid not in await _app_ids()

    await document_service.prepare(vid)                       # the background conversion finishes
    saved = await _stored(mongo, vid)
    assert saved["status"] == "published" and saved["published_at"] and "publish_when_ready" not in saved
    assert saved["document"]["status"] == "ready"
    assert vid in await _app_ids()
    shown = await video_service.get_published(vid, "http://t")
    assert shown.document and shown.document.page_count == 2   # the app gets the document with the video


async def test_failed_document_blocks_publishing_and_cancels_the_wait(storage, mongo):
    from app.core.exceptions import DocumentNotReady
    from app.services import document_service, video_service

    vid = await _video(storage, mongo)
    await mongo["videos"].update_one({"_id": ObjectId(vid)}, {"$set": {
        "document": await _document(storage, vid, data=b"this is not a pdf"), "publish_when_ready": True}})
    await document_service.prepare(vid)                       # fails: not a PDF
    saved = await _stored(mongo, vid)
    assert saved["document"]["status"] == "failed" and saved["status"] == "draft"
    assert "publish_when_ready" not in saved                  # a failed document never publishes the video later
    for when_ready in (False, True):
        with pytest.raises(DocumentNotReady) as e:
            await video_service.set_status(vid, "published", "http://t", when_ready=when_ready)
        assert e.value.details == {"document_status": "failed"}   # no "publish when ready" offer
    assert vid not in await _app_ids()

    # replace it with a good one: ready -> publishing works again
    await document_service.remove(vid)
    assert "document" not in await _stored(mongo, vid)
    assert (await video_service.set_status(vid, "published", "http://t")).visible_in_app


async def test_published_video_with_unready_document_is_hidden_from_the_app(storage, mongo):
    from app.core.exceptions import VideoNotFound
    from app.repositories import video_repo
    from app.services import document_service, video_service

    vid = await _video(storage, mongo, status="published", doc={"status": "processing"})
    assert vid not in await _app_ids()
    with pytest.raises(VideoNotFound):
        await video_service.get_published(vid, "http://t")
    with pytest.raises(VideoNotFound):
        await video_service.published_doc(vid)
    admin = await video_service.get_admin(vid, "http://t")
    assert admin.status == "published" and not admin.visible_in_app          # the dashboard can tell
    await mongo["videos"].update_one({"_id": ObjectId(vid)}, {"$set": {"category": "Recipes"}})
    assert "Recipes" not in await video_repo.categories(published_only=True)

    await document_service.prepare(vid)
    assert vid in await _app_ids()
    assert "Recipes" in await video_repo.categories(published_only=True)
    assert (await video_service.get_admin(vid, "http://t")).visible_in_app


async def test_failed_document_hides_a_published_video_until_fixed(storage, mongo):
    from app.services import document_service, video_service

    vid = await _video(storage, mongo, status="published", doc={"status": "failed"})
    assert vid not in await _app_ids()
    await mongo["videos"].update_one({"_id": ObjectId(vid)}, {"$set": {"document.status": "processing"}})
    await document_service.prepare(vid)
    assert vid in await _app_ids() and (await video_service.get_admin(vid, "http://t")).visible_in_app


async def test_publishing_or_unpublishing_by_hand_ends_the_wait(storage, mongo):
    from app.services import video_service

    vid = await _video(storage, mongo, doc={"status": "processing"})
    await video_service.set_status(vid, "published", "http://t", when_ready=True)
    out = await video_service.set_status(vid, "draft", "http://t")           # "Cancel" in the dashboard
    assert out.status == "draft" and not out.publish_when_ready
    assert "publish_when_ready" not in await _stored(mongo, vid)


async def test_removing_the_document_ends_the_wait(storage, mongo):
    from app.services import document_service, video_service

    vid = await _video(storage, mongo, doc={"status": "processing"})
    await video_service.set_status(vid, "published", "http://t", when_ready=True)
    await document_service.remove(vid)
    saved = await _stored(mongo, vid)
    assert saved["status"] == "draft" and "publish_when_ready" not in saved


async def test_a_document_still_uploading_counts_as_not_ready(storage, mongo):
    from app.core.exceptions import DocumentNotReady
    from app.services import video_service

    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    vid = await _video(storage, mongo, document_upload={"id": "x", "key": "documents/x.pdf", "expires_at": soon})
    with pytest.raises(DocumentNotReady) as e:
        await video_service.set_status(vid, "published", "http://t")
    assert e.value.details["document_status"] == "uploading"
    # an abandoned upload (its time ran out) must not block the video forever
    gone = datetime.now(timezone.utc) - timedelta(minutes=1)
    old = await _video(storage, mongo, document_upload={"id": "y", "key": "documents/y.pdf", "expires_at": gone})
    assert (await video_service.set_status(old, "published", "http://t")).status == "published"


async def test_already_ready_document_publishes_normally_even_with_when_ready(storage, mongo):
    from app.services import video_service

    vid = await _video(storage, mongo, doc={"status": "ready"})
    out = await video_service.set_status(vid, "published", "http://t", when_ready=True)
    assert out.status == "published" and not out.publish_when_ready and out.visible_in_app


async def test_status_request_schema():
    from pydantic import ValidationError
    from app.schemas.video import VideoStatusRequest

    assert VideoStatusRequest(status="published").when_ready is False
    assert VideoStatusRequest(status="published", when_ready=True).when_ready is True
    with pytest.raises(ValidationError):
        VideoStatusRequest(status="published", nonsense=1)
