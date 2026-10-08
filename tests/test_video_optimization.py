"""Video optimization: uploads wait in the server's staging folder, are re-encoded smaller with
ffmpeg, and only the result is stored in the media storage (R2). The original never reaches it."""

import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest
from bson import ObjectId

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"),
]
MB = 1024 * 1024
ADMIN = {"_id": ObjectId()}


# ── fixtures ─────────────────────────────────────────────────────────────────

def _ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:a", "aac", "-b:a", "256k", "-shortest"]
    src = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "6"]
    # constant ~8 Mbit/s: far above what this picture needs
    _ffmpeg(*src, *audio, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "8M", "-minrate", "8M", "-maxrate", "8M",
            "-bufsize", "8M", "-x264-params", "nal-hrd=cbr", str(d / "big.mp4"))
    _ffmpeg(*src, *audio, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "36", str(d / "small.mp4"))
    _ffmpeg(*src, *audio, "-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p", str(d / "mpeg4.mp4"))
    data = os.urandom(300_000)
    (d / "bad.mp4").write_bytes(data[:4] + b"ftyp" + data[8:])
    return {p.stem: p.read_bytes() for p in d.iterdir()}


@pytest.fixture()
def optimizing(storage, mongo, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.providers import storage as storage_pkg

    monkeypatch.setattr(settings, "VIDEO_OPTIMIZE_ENABLED", True)
    monkeypatch.setattr(settings, "VIDEO_STAGING_ROOT", str(tmp_path / "staging"))
    monkeypatch.setattr(settings, "VIDEO_OPTIMIZE_PRESET", "veryfast")
    storage_pkg._staging = None
    yield storage_pkg.get_staging_storage()
    storage_pkg._staging = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def r2_keys(storage, video_id: str) -> list[str]:
    out = storage.client.list_objects_v2(Bucket=storage.bucket, Prefix=f"videos/{video_id}/")
    return [o["Key"] for o in out.get("Contents", [])]


async def _upload(data: bytes, title="Lesson", **kw):
    from app.services import video_service

    v = await video_service.start_upload(
        ADMIN, title=title, description=None, category="Recipes", sort_order=3, file_name="in.mp4",
        file_size=len(data), content_type="video/mp4", base_url="http://t", sha256=_sha(data), **kw)
    size = 5 * MB
    doc = await video_service.get_upload_doc(v.id)
    for i in range(0, len(data), size):
        await video_service.write_chunk(doc, i // size, data[i:i + size], None)
    return await video_service.complete_upload(v.id, "http://t")


async def _row(mongo, vid):
    return await mongo["videos"].find_one({"_id": ObjectId(vid)})


def _probe(path_or_bytes, tmp_path) -> dict:
    p = tmp_path / "p.mp4"
    p.write_bytes(path_or_bytes)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,codec_type:format=duration",
         "-of", "json", str(p)], capture_output=True, check=True).stdout
    import json
    return json.loads(out)


async def _download(storage, key) -> bytes:
    return storage.client.get_object(Bucket=storage.bucket, Key=key)["Body"].read()


# ── tests ────────────────────────────────────────────────────────────────────

async def test_upload_waits_in_staging_and_never_reaches_r2(optimizing, storage, mongo, clips):
    big = clips["big"]
    assert len(big) > 5 * MB                                   # more than one piece
    v = await _upload(big)
    assert v.status == "draft" and v.optimization.state == "queued" and v.optimization.source_size == len(big)
    assert v.playback_url is None and not v.visible_in_app
    row = await _row(mongo, v.id)
    assert row["file"]["staged"] is True
    assert r2_keys(storage, v.id) == []                        # nothing in R2
    assert (optimizing.local_path(row["file"]["key"])).read_bytes() == big


async def test_optimizer_stores_only_the_smaller_file(optimizing, storage, mongo, clips, tmp_path):
    from app.services import optimizer, video_service

    big = clips["big"]
    v = await _upload(big)
    old_key = (await _row(mongo, v.id))["file"]["key"]
    assert await optimizer.run_once() is True and await optimizer.run_once() is False

    row = await _row(mongo, v.id)
    f, o = row["file"], row["optimization"]
    assert "staged" not in f and f["key"] != old_key
    assert o["state"] == "done" and o["mode"] == "compressed" and o["source_size"] == len(big)
    assert o["output_size"] == f["size"] < len(big) * 0.9 and o["saved_percent"] >= 10
    assert f["source_sha256"] == _sha(big) and f["sha256"] != _sha(big) and f["video_codec"] == "h264"
    # R2 holds exactly one object: the optimized file; the staged original is gone
    assert r2_keys(storage, v.id) == [f["key"]]
    assert not optimizing.local_path(old_key).exists()
    stored = await _download(storage, f["key"])
    assert len(stored) == f["size"] and _sha(stored) == f["sha256"]
    info = _probe(stored, tmp_path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 640, 360)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - 6) < 0.3
    # untouched details; playable now
    assert row["title"] == "Lesson" and row["category"] == "Recipes" and row["sort_order"] == 3 and row["status"] == "draft"
    out = await video_service.get_admin(v.id, "http://t")
    assert out.playback_url and out.optimization.saved_percent == o["saved_percent"] and out.file_size == f["size"]


async def test_already_efficient_h264_is_kept_as_it_is(optimizing, storage, mongo, clips):
    from app.services import optimizer

    small = clips["small"]
    v = await _upload(small)
    await optimizer.run_once()
    row = await _row(mongo, v.id)
    assert row["optimization"]["mode"] == "kept" and row["optimization"]["state"] == "done"
    assert row["file"]["size"] == len(small) and row["file"]["sha256"] == _sha(small)
    assert "staged" not in row["file"]
    assert (await _download(storage, row["file"]["key"])) == small
    assert list((optimizing.staging_dir()).glob("*")) == []     # nothing left behind in staging


async def test_other_codecs_are_converted_to_h264(optimizing, storage, mongo, clips, tmp_path):
    from app.services import optimizer

    v = await _upload(clips["mpeg4"])
    await optimizer.run_once()
    row = await _row(mongo, v.id)
    assert row["optimization"]["mode"] == "converted" and row["file"]["video_codec"] == "h264"
    info = _probe(await _download(storage, row["file"]["key"]), tmp_path)
    assert next(s for s in info["streams"] if s["codec_type"] == "video")["codec_name"] == "h264"


async def test_a_file_that_cannot_be_converted_fails_and_stays_off_r2(optimizing, storage, mongo, clips):
    from app.services import optimizer, video_service

    v = await _upload(clips["bad"])
    await optimizer.run_once()
    row = await _row(mongo, v.id)
    assert row["optimization"]["state"] == "failed" and row["optimization"]["error"]
    assert row["file"]["staged"] is True and optimizing.local_path(row["file"]["key"]).is_file()
    assert r2_keys(storage, v.id) == []
    out = await video_service.get_admin(v.id, "http://t")
    assert out.optimization.state == "failed" and out.playback_url is None
    # retry queues it again (and fails again for the same reason)
    again = await video_service.retry_optimization(v.id, "http://t")
    assert again.optimization.state == "queued" and again.optimization.error is None
    await optimizer.run_once()
    assert (await _row(mongo, v.id))["optimization"]["state"] == "failed"


async def test_retry_only_for_failed(optimizing, storage, mongo, clips):
    from app.core.exceptions import VideoNotReady
    from app.services import video_service

    v = await _upload(clips["big"])
    with pytest.raises(VideoNotReady):
        await video_service.retry_optimization(v.id, "http://t")


async def test_cannot_publish_while_optimizing_but_can_wait(optimizing, storage, mongo, clips):
    from app.core.exceptions import VideoOptimizing
    from app.services import optimizer, video_service

    v = await _upload(clips["big"])
    with pytest.raises(VideoOptimizing) as e:
        await video_service.set_status(v.id, "published", "http://t")
    assert e.value.details["can_publish_when_ready"] is True
    waiting = await video_service.set_status(v.id, "published", "http://t", when_ready=True)
    assert waiting.status == "draft" and waiting.publish_when_ready is True and not waiting.visible_in_app
    await optimizer.run_once()
    row = await _row(mongo, v.id)
    assert row["status"] == "published" and "publish_when_ready" not in row and row["published_at"]
    assert (await video_service.get_admin(v.id, "http://t")).visible_in_app


async def test_failed_optimization_blocks_publishing(optimizing, storage, mongo, clips):
    from app.core.exceptions import VideoOptimizing
    from app.services import optimizer, video_service

    v = await _upload(clips["bad"])
    await optimizer.run_once()
    for when_ready in (False, True):
        with pytest.raises(VideoOptimizing) as e:
            await video_service.set_status(v.id, "published", "http://t", when_ready=when_ready)
        assert e.value.details["optimization_state"] == "failed"


async def test_publish_waits_for_document_and_optimization_both(optimizing, storage, mongo, clips):
    from app.repositories import video_repo
    from app.services import optimizer, video_service

    v = await _upload(clips["big"])
    await mongo["videos"].update_one({"_id": ObjectId(v.id)}, {"$set": {
        "document": {"id": "d", "status": "processing", "key": f"documents/{v.id}/{'a' * 32}.pdf", "name": "d.pdf",
                     "file_type": "pdf", "size": 10, "uploaded_at": datetime.now(timezone.utc)}}})
    await video_service.set_status(v.id, "published", "http://t", when_ready=True)
    await optimizer.run_once()                                       # video ready, document still processing
    assert (await _row(mongo, v.id))["status"] == "draft"
    await mongo["videos"].update_one({"_id": ObjectId(v.id)}, {"$set": {"document.status": "ready"}})
    assert await video_repo.publish_if_waiting(ObjectId(v.id))       # the document finishing publishes it
    assert (await _row(mongo, v.id))["status"] == "published"


async def test_a_staged_video_is_never_shown_in_the_app(optimizing, storage, mongo, clips):
    from app.services import video_service

    v = await _upload(clips["big"])
    await mongo["videos"].update_one({"_id": ObjectId(v.id)}, {"$set": {"status": "published"}})   # even if forced
    page = await video_service.list_published(None, 1, 20, "http://t")
    assert page.total == 0


async def test_deleting_removes_the_staged_original(optimizing, storage, mongo, clips):
    from app.services import video_service

    v = await _upload(clips["big"])
    key = (await _row(mongo, v.id))["file"]["key"]
    assert optimizing.local_path(key).is_file()
    await video_service.delete_video(v.id)
    assert not optimizing.local_path(key).exists() and r2_keys(storage, v.id) == []


async def test_deleted_while_waiting_is_skipped_and_while_running_leaves_nothing(optimizing, storage, mongo, clips):
    from app.repositories import video_repo
    from app.services import optimizer, video_service

    v = await _upload(clips["big"])
    doc = await video_repo.claim_optimization()                      # the worker has taken it …
    await video_service.delete_video(v.id)                           # … and the admin deletes the video meanwhile
    await optimizer.run_job(doc)
    assert r2_keys(storage, v.id) == [] and await _row(mongo, v.id) is None
    assert list(optimizing.staging_dir().glob("*")) == []


async def test_interrupted_jobs_are_queued_again(optimizing, storage, mongo, clips):
    from app.repositories import video_repo
    from app.services import optimizer

    v = await _upload(clips["big"])
    await video_repo.claim_optimization()
    assert (await _row(mongo, v.id))["optimization"]["state"] == "running"
    assert await video_repo.requeue_running_optimizations() == 1
    await optimizer.run_once()
    assert (await _row(mongo, v.id))["optimization"]["state"] == "done"


async def test_progress_is_reported_while_encoding(optimizing, storage, mongo, clips, monkeypatch):
    from app.services import optimizer

    v = await _upload(clips["big"])
    seen = []
    original = optimizer._set

    async def spy(video_id, fields):
        if "progress" in fields:
            seen.append(fields["progress"])
        await original(video_id, fields)

    monkeypatch.setattr(optimizer, "_set", spy)
    await optimizer.run_once()
    assert seen and all(0 <= p < 1 for p in seen) and seen == sorted(seen)


async def test_replacement_file_is_optimized_then_swapped_in(optimizing, storage, mongo, clips):
    from app.services import optimizer, video_service

    first = await _upload(clips["small"], title="Original")
    await optimizer.run_once()
    target = await _row(mongo, first.id)
    old_key = target["file"]["key"]

    staging = await video_service.start_replacement(
        first.id, ADMIN, file_name="new.mp4", file_size=len(clips["big"]), content_type="video/mp4",
        base_url="http://t", sha256=_sha(clips["big"]))
    doc = await video_service.get_upload_doc(staging.id)
    for i in range(0, len(clips["big"]), 5 * MB):
        await video_service.write_chunk(doc, i // (5 * MB), clips["big"][i:i + 5 * MB], None)
    done = await video_service.complete_upload(staging.id, "http://t")
    assert done.id == staging.id and done.optimization.state == "queued"
    mid = await video_service.get_admin(first.id, "http://t")
    assert mid.replacement.optimization == "queued"                  # the old file keeps playing meanwhile
    assert (await _row(mongo, first.id))["file"]["key"] == old_key and await storage.exists(old_key)

    await optimizer.run_once()
    row = await _row(mongo, first.id)
    assert row["file"]["key"] != old_key and "replacement" not in row and row["title"] == "Original"
    assert row["optimization"]["mode"] == "compressed" and row["file"]["source_sha256"] == _sha(clips["big"])
    assert not await storage.exists(old_key)
    # the new file lives under the upload's id; nothing else is left under either id
    assert r2_keys(storage, staging.id) == [row["file"]["key"]] and r2_keys(storage, first.id) == []
    assert await mongo["videos"].count_documents({}) == 1


async def test_cannot_replace_a_file_that_is_still_being_optimized(optimizing, storage, mongo, clips):
    from app.core.exceptions import VideoNotReady
    from app.services import video_service

    v = await _upload(clips["big"])
    with pytest.raises(VideoNotReady):
        await video_service.start_replacement(
            v.id, ADMIN, file_name="n.mp4", file_size=1000, content_type="video/mp4", base_url="http://t")


async def test_a_full_staging_disk_refuses_the_upload(optimizing, storage, mongo, clips, monkeypatch):
    from app.core.exceptions import InsufficientStorage
    from app.services import video_service

    monkeypatch.setattr(type(optimizing), "free_bytes", lambda self: 50 * MB)
    with pytest.raises(InsufficientStorage):
        await video_service.start_upload(
            ADMIN, title="Big", description=None, category=None, sort_order=0, file_name="b.mp4",
            file_size=100 * MB, content_type="video/mp4", base_url="http://t")


async def test_staging_space_counts_uploads_still_arriving(optimizing, storage, mongo, clips, monkeypatch):
    from app.core.exceptions import InsufficientStorage
    from app.services import video_service

    monkeypatch.setattr(type(optimizing), "free_bytes", lambda self: 2400 * MB)
    await video_service.start_upload(          # 800 MB announced, nothing received yet
        ADMIN, title="One", description=None, category=None, sort_order=0, file_name="a.mp4",
        file_size=800 * MB, content_type="video/mp4", base_url="http://t")
    with pytest.raises(InsufficientStorage):   # the disk looks free, but the first upload will fill it
        await video_service.start_upload(
            ADMIN, title="Two", description=None, category=None, sort_order=0, file_name="b.mp4",
            file_size=800 * MB, content_type="video/mp4", base_url="http://t")


async def test_the_optimized_video_still_counts_as_a_duplicate_of_its_original(optimizing, storage, mongo, clips):
    from app.core.exceptions import DuplicateVideo
    from app.services import optimizer, video_service

    big = clips["big"]
    await _upload(big)
    await optimizer.run_once()
    with pytest.raises(DuplicateVideo):
        await video_service.start_upload(
            ADMIN, title="Again", description=None, category=None, sort_order=0, file_name="x.mp4",
            file_size=len(big), content_type="video/mp4", base_url="http://t", sha256=_sha(big))


async def test_storage_check_looks_in_staging_for_waiting_videos(optimizing, storage, mongo, clips):
    from app.services import video_service

    v = await _upload(clips["big"])
    check = await video_service.storage_check()
    assert check.broken == []
    os.unlink(optimizing.local_path((await _row(mongo, v.id))["file"]["key"]))
    assert [b.id for b in (await video_service.storage_check()).broken] == [v.id]


async def test_optimization_off_uses_the_direct_path(storage, mongo, clips, monkeypatch):
    from app.core.config import settings
    from app.services import video_service

    monkeypatch.setattr(settings, "VIDEO_OPTIMIZE_ENABLED", False)
    v = await _upload(clips["small"])
    assert v.optimization is None and v.status == "draft" and v.playback_url
    assert "staged" not in (await _row(mongo, v.id))["file"]


async def test_status_endpoint_data(optimizing):
    from app.services import video_service

    s = video_service.optimization_status()
    assert s.enabled and s.available and s.active and s.crf == 21 and s.preset == "veryfast"
