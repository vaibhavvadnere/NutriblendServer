"""Length and codec read by the admin's browser before upload: stored when the upload
starts, kept if the server can't probe the file itself, replaced by ffprobe when it can."""

import hashlib
import os

import pytest
from bson import ObjectId
from pydantic import ValidationError

pytestmark = pytest.mark.asyncio
MB = 1024 * 1024


def _data(size=6 * MB) -> bytes:
    data = os.urandom(size)
    return data[:4] + b"ftyp" + data[8:]


async def _start(data: bytes, **facts):
    from app.services import video_service

    return await video_service.start_upload(
        {"_id": ObjectId()}, title="Clip", description=None, category=None, sort_order=0,
        file_name="clip.mp4", file_size=len(data), content_type="video/mp4", base_url="http://t",
        sha256=hashlib.sha256(data).hexdigest(), **facts,
    )


async def _finish(video_id: str, data: bytes):
    from app.services import video_service

    size = 5 * MB
    doc = await video_service.get_upload_doc(video_id)
    for i in range(0, len(data), size):
        await video_service.write_chunk(doc, i // size, data[i:i + size], None)
    return await video_service.complete_upload(video_id, "http://t")


async def test_browser_facts_are_stored_at_start(storage, mongo):
    v = await _start(_data(), duration_seconds=754.337, video_codec="h264")
    assert v.duration_seconds == 754.34 and v.video_codec == "h264"
    saved = await mongo["videos"].find_one({})
    assert saved["file"]["duration_seconds"] == 754.34 and saved["file"]["video_codec"] == "h264"


async def test_no_facts_means_nothing_stored(storage, mongo):
    v = await _start(_data())
    assert v.duration_seconds is None and v.video_codec is None


async def test_facts_survive_completion_when_server_cannot_probe(storage, mongo, monkeypatch):
    from app.services import video_service

    async def no_ffprobe(_path):
        return {}

    monkeypatch.setattr(video_service, "_probe", no_ffprobe)
    data = _data()
    v = await _start(data, duration_seconds=12.5, video_codec="h264")
    done = await _finish(v.id, data)
    assert done.status == "draft" and done.duration_seconds == 12.5 and done.video_codec == "h264"


async def test_ffprobe_overrules_browser_facts(storage, mongo, monkeypatch):
    from app.services import video_service

    async def probed(_path):
        return {"duration_seconds": 13.04, "video_codec": "hevc"}

    monkeypatch.setattr(video_service, "_probe", probed)
    data = _data()
    v = await _start(data, duration_seconds=12.5, video_codec="h264")
    done = await _finish(v.id, data)
    assert done.duration_seconds == 13.04 and done.video_codec == "hevc"


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_request_validation():
    from app.schemas.video import VideoCreateRequest

    base = dict(title="T", file_name="a.mp4", file_size=10, content_type="video/mp4")
    ok = VideoCreateRequest(**base, duration_seconds=90.5, video_codec="h264")
    assert ok.duration_seconds == 90.5 and ok.video_codec == "h264"
    for bad in ({"duration_seconds": 0}, {"duration_seconds": -3}, {"duration_seconds": 90_000},
                {"video_codec": "H.264 / High"}, {"video_codec": "x" * 21}):
        with pytest.raises(ValidationError):
            VideoCreateRequest(**base, **bad)
