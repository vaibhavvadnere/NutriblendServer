"""Replacing a video's file: the old file keeps playing while the new one uploads; completing
the upload swaps the file in (record, thumbnail, document, order, publish status stay) and
deletes the old one."""

import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio
MB = 1024 * 1024
ADMIN = {"_id": ObjectId()}


def _mp4(size=6 * MB) -> bytes:
    data = os.urandom(size)
    return data[:4] + b"\x00\x00\x00\x00"[:0] + b"ftyp" + data[8:]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _send(video_id: str, data: bytes) -> None:
    from app.services import video_service

    size = 5 * MB
    doc = await video_service.get_upload_doc(video_id)
    for i in range(0, len(data), size):
        await video_service.write_chunk(doc, i // size, data[i:i + size], None)


async def _video(storage, mongo, data=None, **fields):
    """A finished (draft) video with its file in storage."""
    from app.services import video_service

    data = data or _mp4()
    v = await video_service.start_upload(
        ADMIN, title=fields.pop("title", "Lesson"), description="d", category="Recipes", sort_order=7,
        file_name="old.mp4", file_size=len(data), content_type="video/mp4", base_url="http://t", sha256=_sha(data))
    await _send(v.id, data)
    done = await video_service.complete_upload(v.id, "http://t")
    if fields:
        await mongo["videos"].update_one({"_id": ObjectId(v.id)}, {"$set": fields})
    return done, data


async def _replace(video_id: str, data: bytes, **kw):
    from app.services import video_service

    return await video_service.start_replacement(
        video_id, ADMIN, file_name="new.mp4", file_size=len(data), content_type="video/mp4", base_url="http://t",
        sha256=_sha(data), **kw)


async def _row(mongo, vid):
    return await mongo["videos"].find_one({"_id": ObjectId(vid)})


async def test_replace_keeps_the_video_and_swaps_the_file(storage, mongo):
    from app.services import video_service

    video, old = await _video(storage, mongo)
    now = datetime.now(timezone.utc)
    await mongo["videos"].update_one({"_id": ObjectId(video.id)}, {"$set": {
        "status": "published", "published_at": now,
        "thumbnail": {"key": f"thumbnails/{video.id}/{'a' * 32}.jpg", "size": 3, "content_type": "image/jpeg"}}})
    before = await _row(mongo, video.id)
    old_key, published_at = before["file"]["key"], before["published_at"]

    new = _mp4(7 * MB)
    staging = await _replace(video.id, new)
    assert staging.status == "uploading" and staging.replaces == video.id and staging.id != video.id

    # while the new file uploads, the video still has (and plays) its old file
    mid = await video_service.get_admin(video.id, "http://t")
    assert mid.replacement and mid.replacement.video_id == staging.id and mid.replacement.file_size == len(new)
    assert (await _row(mongo, video.id))["file"]["key"] == old_key and await storage.exists(old_key)
    assert mid.status == "published" and mid.visible_in_app
    listing = await video_service.list_admin(None, None, None, 1, 50, "http://t")
    assert [v.id for v in listing.items] == [video.id]                       # the unfinished upload isn't a video of its own
    assert (await video_service.counts())["uploading"] == 0

    await _send(staging.id, new)
    done = await video_service.complete_upload(staging.id, "http://t")
    assert done.id == video.id                                                # the video comes back, not the upload
    row = await _row(mongo, video.id)
    assert row["file"]["key"] != old_key and row["file"]["size"] == len(new) and row["file"]["sha256"] == _sha(new)
    assert row["file"]["original_name"] == "new.mp4" and row["file"]["integrity_verified"] is True
    # everything else stays
    assert row["title"] == "Lesson" and row["category"] == "Recipes" and row["sort_order"] == 7
    assert row["status"] == "published" and row["published_at"] == published_at and row["thumbnail"]["size"] == 3
    assert "replacement" not in row and done.replacement is None
    # old file deleted, new one in place, the upload record is gone
    assert not await storage.exists(old_key) and await storage.exists(row["file"]["key"])
    assert await _row(mongo, staging.id) is None
    assert await mongo["videos"].count_documents({}) == 1


async def test_same_file_is_a_duplicate_unless_allowed(storage, mongo):
    from app.core.exceptions import DuplicateVideo

    video, data = await _video(storage, mongo)
    with pytest.raises(DuplicateVideo) as e:
        await _replace(video.id, data)
    assert e.value.details["video_id"] == video.id
    assert (await _row(mongo, video.id)).get("replacement") is None
    staging = await _replace(video.id, data, allow_duplicate=True)
    assert staging.replaces == video.id


async def test_choosing_the_same_file_again_resumes_and_another_file_discards(storage, mongo):
    from app.services import video_service

    video, _ = await _video(storage, mongo)
    new = _mp4(7 * MB)
    first = await _replace(video.id, new)
    again = await _replace(video.id, new)
    assert again.id == first.id                                               # same file: carry on with that upload
    assert await mongo["videos"].count_documents({}) == 2

    other = _mp4(6 * MB)
    second = await _replace(video.id, other)
    assert second.id != first.id and await _row(mongo, first.id) is None      # another file: the unfinished one is dropped
    assert (await video_service.get_admin(video.id, "http://t")).replacement.video_id == second.id
    assert await mongo["videos"].count_documents({}) == 2


async def test_replacing_needs_a_finished_video(storage, mongo):
    from app.core.exceptions import VideoNotReady
    from app.services import video_service

    data = _mp4()
    unfinished = await video_service.start_upload(
        ADMIN, title="U", description=None, category=None, sort_order=0, file_name="u.mp4", file_size=len(data),
        content_type="video/mp4", base_url="http://t", sha256=_sha(data))
    with pytest.raises(VideoNotReady):
        await _replace(unfinished.id, _mp4(6 * MB))
    video, _ = await _video(storage, mongo)
    staging = await _replace(video.id, _mp4(6 * MB))
    with pytest.raises(VideoNotReady):                                        # an upload can't be replaced itself
        await _replace(staging.id, _mp4(6 * MB))


async def test_incomplete_or_invalid_replacement_leaves_the_video_alone(storage, mongo):
    from app.core.exceptions import InvalidMediaFile, UploadIncomplete
    from app.services import video_service

    video, _ = await _video(storage, mongo)
    old_key = (await _row(mongo, video.id))["file"]["key"]
    new = _mp4(7 * MB)
    staging = await _replace(video.id, new)
    doc = await video_service.get_upload_doc(staging.id)
    await video_service.write_chunk(doc, 0, new[:5 * MB], None)
    with pytest.raises(UploadIncomplete):
        await video_service.complete_upload(staging.id, "http://t")
    assert (await _row(mongo, video.id))["file"]["key"] == old_key

    bad = b"this is not a video at all" + os.urandom(6 * MB)
    s2 = await _replace(video.id, bad)
    with pytest.raises(InvalidMediaFile):                                     # refused as soon as the first piece arrives
        await _send(s2.id, bad)
    row = await _row(mongo, video.id)
    assert row["file"]["key"] == old_key and await storage.exists(old_key) and row["replacement"]["id"] == s2.id


async def test_cancelling_the_replacement_and_deleting_the_video(storage, mongo):
    from app.services import video_service

    video, _ = await _video(storage, mongo)
    old_key = (await _row(mongo, video.id))["file"]["key"]
    staging = await _replace(video.id, _mp4(6 * MB))
    await video_service.delete_video(staging.id)                              # "Discard replacement"
    row = await _row(mongo, video.id)
    assert row and "replacement" not in row and row["file"]["key"] == old_key and await storage.exists(old_key)

    staging = await _replace(video.id, _mp4(6 * MB))
    await video_service.delete_video(video.id)                                # deleting the video drops its replacement too
    assert await mongo["videos"].count_documents({}) == 0
    assert not await storage.exists(old_key)


async def test_expired_replacement_is_cleaned_up(storage, mongo):
    from app.services import video_service

    video, _ = await _video(storage, mongo)
    staging = await _replace(video.id, _mp4(6 * MB))
    await mongo["videos"].update_one({"_id": ObjectId(staging.id)},
                                     {"$set": {"upload.expires_at": datetime.now(timezone.utc) - timedelta(hours=1)}})
    assert await video_service.cleanup_expired_uploads() == 1
    row = await _row(mongo, video.id)
    assert row and "replacement" not in row
    assert (await video_service.get_admin(video.id, "http://t")).replacement is None


async def test_a_leftover_finished_record_never_takes_the_videos_file_along(storage, mongo):
    """If the server stopped between swapping and removing the upload record, deleting that
    leftover must not delete the file the video now uses."""
    from app.services import video_service

    video, _ = await _video(storage, mongo)
    row = await _row(mongo, video.id)
    leftover = ObjectId()
    now = datetime.now(timezone.utc)
    await mongo["videos"].insert_one({"_id": leftover, "title": "x (new file)", "status": "draft", "sort_order": 0,
                                      "created_at": now, "updated_at": now, "replaces": row["_id"], "file": dict(row["file"])})
    await video_service.delete_video(str(leftover))
    assert await storage.exists(row["file"]["key"])
    assert await _row(mongo, video.id) is not None


async def test_target_deleted_before_completion(storage, mongo):
    from app.core.exceptions import VideoNotFound
    from app.services import video_service

    video, _ = await _video(storage, mongo)
    new = _mp4(6 * MB)
    staging = await _replace(video.id, new)
    await _send(staging.id, new)
    await mongo["videos"].delete_one({"_id": ObjectId(video.id)})             # removed by hand meanwhile
    with pytest.raises(VideoNotFound):
        await video_service.complete_upload(staging.id, "http://t")
    assert await mongo["videos"].count_documents({}) == 0


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_request_schema():
    from pydantic import ValidationError
    from app.schemas.video import VideoReplaceRequest

    base = dict(file_name="a.mp4", file_size=10, content_type="video/mp4")
    assert VideoReplaceRequest(**base).allow_duplicate is False
    for bad in ({"file_size": 0}, {"sha256": "xyz"}, {"duration_seconds": 0}, {"title": "no such field"}):
        with pytest.raises(ValidationError):
            VideoReplaceRequest(**{**base, **bad})
