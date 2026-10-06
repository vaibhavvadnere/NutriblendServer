"""
services/video_service.py — Videos: chunked upload, details, publishing, playback links.

Upload protocol (resumable; every step is safe to retry):

    1. POST   /admin/videos                        details + file name/size/type
                                                   -> video (status "uploading") + chunk plan
    2. PUT    /admin/videos/{id}/upload/chunks/{n}  raw bytes of chunk n (any order, re-send OK)
       GET    /admin/videos/{id}/upload             which chunks are still missing (resume)
    3. POST   /admin/videos/{id}/upload/complete    -> status "draft"
    4. PATCH  /admin/videos/{id}/status             "published" -> visible in the app

Unfinished uploads expire after UPLOAD_SESSION_TTL_HOURS and are deleted,
files included (on startup and whenever a new upload starts).
"""

import asyncio
import json
import logging
import math
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId

from app.core import media_links
from app.core.config import settings
from app.core.exceptions import (
    FileTooLarge,
    InsufficientStorage,
    InvalidMediaFile,
    UnsupportedMediaType,
    UploadIncomplete,
    UploadInvalidChunk,
    UploadNotInProgress,
    VideoNotFound,
    VideoNotReady,
)
from app.providers.storage import get_storage
from app.repositories import video_repo
from app.services import document_service
from app.schemas.video import (
    ALLOWED_IMAGE_TYPES,
    ALLOWED_VIDEO_TYPES,
    AdminVideoOut,
    AdminVideoPage,
    UploadProgress,
    VideoOut,
    VideoPage,
)

logger = logging.getLogger("nutriblend.videos")

STATUS_UPLOADING = "uploading"
STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"

MB = 1024 * 1024
MAX_PAGE_SIZE = 100
_FREE_SPACE_MARGIN = 200 * MB


def chunk_size() -> int:
    return settings.UPLOAD_CHUNK_SIZE_MB * MB


# ── Output shaping ───────────────────────────────────────────────────────────

def _links(doc: dict, base_url: str) -> dict[str, Any]:
    if doc["status"] == STATUS_UPLOADING:
        return {}
    ttl = settings.MEDIA_URL_TTL_SECONDS
    out = {
        "playback_url": media_links.signed_url(base_url, doc["file"]["key"], ttl),
        "links_expire_at": datetime.now(timezone.utc) + timedelta(seconds=ttl),
    }
    if doc.get("thumbnail"):
        out["thumbnail_url"] = media_links.signed_url(base_url, doc["thumbnail"]["key"], ttl)
    return out


def _common(doc: dict) -> dict[str, Any]:
    f = doc["file"]
    return {
        "id": str(doc["_id"]),
        "title": doc["title"],
        "description": doc.get("description"),
        "category": doc.get("category"),
        "sort_order": doc.get("sort_order", 0),
        "duration_seconds": f.get("duration_seconds"),
        "file_size": f["size"],
        "content_type": f["content_type"],
        "published_at": doc.get("published_at"),
    }


def _progress(doc: dict) -> Optional[UploadProgress]:
    up = doc.get("upload")
    if not up or doc["status"] != STATUS_UPLOADING:
        return None
    received = set(up.get("received", []))
    missing = [i for i in range(up["total_chunks"]) if i not in received]
    return UploadProgress(
        chunk_size=up["chunk_size"],
        total_chunks=up["total_chunks"],
        received_chunks=len(received),
        missing_chunks=missing[:1000],
        complete=not missing,
        expires_at=up["expires_at"],
    )


def to_admin_out(doc: dict, base_url: str) -> AdminVideoOut:
    return AdminVideoOut(
        **_common(doc),
        **_links(doc, base_url),
        status=doc["status"],
        original_file_name=doc["file"].get("original_name"),
        video_codec=doc["file"].get("video_codec"),
        has_thumbnail=bool(doc.get("thumbnail")),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        uploaded_at=doc.get("uploaded_at"),
        created_by=str(doc["created_by"]) if doc.get("created_by") else None,
        upload=_progress(doc),
        admin_document=document_service.admin_info(doc),
    )


def to_app_out(doc: dict, base_url: str) -> VideoOut:
    return VideoOut(**_common(doc), **_links(doc, base_url), document=document_service.app_info(doc))


def _pages(total: int, page_size: int) -> int:
    return max(math.ceil(total / page_size), 1)


# ── Lookups ──────────────────────────────────────────────────────────────────

async def _get(video_id: str) -> dict:
    doc = await video_repo.find_by_id(video_id)
    if not doc:
        raise VideoNotFound()
    return doc


async def get_admin(video_id: str, base_url: str) -> AdminVideoOut:
    return to_admin_out(await _get(video_id), base_url)


async def published_doc(video_id: str) -> dict:
    doc = await video_repo.find_by_id(video_id)
    if not doc or doc["status"] != STATUS_PUBLISHED:
        raise VideoNotFound()
    return doc


async def get_published(video_id: str, base_url: str) -> VideoOut:
    doc = await video_repo.find_by_id(video_id)
    if not doc or doc["status"] != STATUS_PUBLISHED:
        raise VideoNotFound()
    return to_app_out(doc, base_url)


async def list_admin(
    query: Optional[str], status: Optional[str], category: Optional[str], page: int, page_size: int, base_url: str
) -> AdminVideoPage:
    page, page_size = max(page, 1), min(max(page_size, 1), MAX_PAGE_SIZE)
    docs, total = await video_repo.search_admin(query, status, category, (page - 1) * page_size, page_size)
    return AdminVideoPage(
        items=[to_admin_out(d, base_url) for d in docs],
        total=total, page=page, page_size=page_size, pages=_pages(total, page_size),
    )


async def list_published(category: Optional[str], page: int, page_size: int, base_url: str) -> VideoPage:
    page, page_size = max(page, 1), min(max(page_size, 1), MAX_PAGE_SIZE)
    docs, total = await video_repo.search_published(category, (page - 1) * page_size, page_size)
    return VideoPage(
        items=[to_app_out(d, base_url) for d in docs],
        total=total, page=page, page_size=page_size, pages=_pages(total, page_size),
    )


async def categories(published_only: bool) -> list[str]:
    return await video_repo.categories(published_only)


# ── Upload ───────────────────────────────────────────────────────────────────

def _validate_video_file(file_name: str, file_size: int, content_type: str) -> None:
    content_type = content_type.lower().strip()
    extensions = ALLOWED_VIDEO_TYPES.get(content_type)
    if not extensions:
        raise UnsupportedMediaType(
            "Only MP4 videos (video/mp4) are supported.", details={"allowed": sorted(ALLOWED_VIDEO_TYPES)}
        )
    if not file_name.lower().endswith(extensions):
        raise UnsupportedMediaType(
            f"The file name must end with {' or '.join(extensions)}.", details={"allowed_extensions": list(extensions)}
        )
    limit = settings.MAX_VIDEO_SIZE_MB * MB
    if file_size > limit:
        raise FileTooLarge(
            f"Videos can be at most {settings.MAX_VIDEO_SIZE_MB} MB.",
            details={"max_bytes": limit, "file_size": file_size},
        )


async def start_upload(
    admin: dict,
    *,
    title: str,
    description: Optional[str],
    category: Optional[str],
    sort_order: int,
    file_name: str,
    file_size: int,
    content_type: str,
    base_url: str,
) -> AdminVideoOut:
    _validate_video_file(file_name, file_size, content_type)
    await cleanup_expired_uploads()

    storage = get_storage()
    free = storage.free_bytes()
    if free is not None and free < file_size + _FREE_SPACE_MARGIN:
        raise InsufficientStorage(details={"needed_bytes": file_size, "free_bytes": free})

    video_id = ObjectId()
    key = f"videos/{video_id}/{uuid.uuid4().hex}.mp4"
    size = chunk_size()
    now = datetime.now(timezone.utc)

    await storage.begin_upload(key, file_size)
    doc = {
        "_id": video_id,
        "title": title,
        "description": description,
        "category": category,
        "sort_order": sort_order,
        "status": STATUS_UPLOADING,
        "file": {
            "key": key,
            "size": file_size,
            "content_type": content_type.lower().strip(),
            "original_name": file_name[-255:],
        },
        "upload": {
            "chunk_size": size,
            "total_chunks": math.ceil(file_size / size),
            "received": [],
            "state": "receiving",
            "expires_at": now + timedelta(hours=settings.UPLOAD_SESSION_TTL_HOURS),
        },
        "created_by": admin["_id"],
        "created_at": now,
        "updated_at": now,
    }
    try:
        await video_repo.insert(doc)
    except Exception:
        await storage.abort_upload(key)
        raise
    logger.info("Upload started: video=%s size=%d chunks=%d", video_id, file_size, doc["upload"]["total_chunks"])
    return to_admin_out(doc, base_url)


def _uploading(doc: dict) -> dict:
    up = doc.get("upload")
    if doc["status"] != STATUS_UPLOADING or not up:
        raise UploadNotInProgress()
    expires_at = up["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise UploadNotInProgress("This upload has expired. Delete the video and start again.")
    return up


def expected_chunk_length(doc: dict, index: int) -> int:
    up = doc["upload"]
    if index < 0 or index >= up["total_chunks"]:
        raise UploadInvalidChunk(
            f"Chunk number must be between 0 and {up['total_chunks'] - 1}.",
            details={"total_chunks": up["total_chunks"]},
        )
    if index < up["total_chunks"] - 1:
        return up["chunk_size"]
    return doc["file"]["size"] - up["chunk_size"] * (up["total_chunks"] - 1)


async def get_upload_doc(video_id: str) -> dict:
    """The video, checked to be receiving chunks (used by the router before reading the body)."""
    doc = await _get(video_id)
    up = _uploading(doc)
    if up.get("state") != "receiving":
        raise UploadNotInProgress("This upload is being finalised.")
    return doc


async def write_chunk(doc: dict, index: int, data: bytes) -> UploadProgress:
    expected = expected_chunk_length(doc, index)
    if len(data) != expected:
        raise UploadInvalidChunk(
            f"Chunk {index} must be exactly {expected} bytes (got {len(data)}).",
            details={"expected_bytes": expected, "received_bytes": len(data)},
        )
    if index == 0 and data[4:8] != b"ftyp":
        # Every MP4 starts with an 'ftyp' box: a renamed .mov/.avi/.txt fails here.
        raise InvalidMediaFile("This file is not a valid MP4 video.")

    await get_storage().write_chunk(doc["file"]["key"], index * doc["upload"]["chunk_size"], data)
    updated = await video_repo.mark_chunk_received(doc["_id"], index)
    if not updated:
        raise UploadNotInProgress()
    return _progress(updated)


async def upload_progress(video_id: str) -> UploadProgress:
    doc = await _get(video_id)
    _uploading(doc)
    return _progress(doc)


async def complete_upload(video_id: str, base_url: str) -> AdminVideoOut:
    doc = await _get(video_id)
    _uploading(doc)
    claimed = await video_repo.claim_for_assembly(doc["_id"])
    if not claimed:
        raise UploadNotInProgress("This upload is already being finalised.")

    progress = _progress(claimed)
    if not progress.complete:
        await video_repo.release_assembly(doc["_id"])
        raise UploadIncomplete(
            f"{progress.total_chunks - progress.received_chunks} of {progress.total_chunks} chunks are still missing.",
            details={"missing_chunks": progress.missing_chunks[:50]},
        )

    storage = get_storage()
    key = claimed["file"]["key"]
    try:
        head = await storage.read_upload_head(key, 12)
        if head[4:8] != b"ftyp":
            raise InvalidMediaFile("This file is not a valid MP4 video.")
        await storage.complete_upload(key)
    except Exception:
        await video_repo.release_assembly(doc["_id"])
        raise

    probe = await _probe(storage.local_path(key))
    now = datetime.now(timezone.utc)
    updated = await video_repo.update(
        doc["_id"],
        {
            "status": STATUS_DRAFT,
            "uploaded_at": now,
            "updated_at": now,
            **{f"file.{k}": v for k, v in probe.items()},
        },
        unset_fields=["upload"],
    )
    logger.info("Upload complete: video=%s probe=%s", video_id, probe or "unavailable")
    return to_admin_out(updated, base_url)


async def _probe(path) -> dict[str, Any]:
    """Duration and video codec via ffprobe, when it is installed. Optional."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or path is None:
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        info = json.loads(out or b"{}")
    except Exception as exc:  # noqa: BLE001 — probing must never fail an upload
        logger.warning("ffprobe failed: %s", exc)
        return {}
    result: dict[str, Any] = {}
    try:
        result["duration_seconds"] = round(float(info["format"]["duration"]), 2)
    except (KeyError, TypeError, ValueError):
        pass
    codec = next((s.get("codec_name") for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if codec:
        result["video_codec"] = codec
    return result


# ── Details, publishing, thumbnail, delete ───────────────────────────────────

async def update_details(video_id: str, changes: dict[str, Any], base_url: str) -> AdminVideoOut:
    doc = await _get(video_id)
    if not changes:
        return to_admin_out(doc, base_url)
    changes["updated_at"] = datetime.now(timezone.utc)
    return to_admin_out(await video_repo.update(doc["_id"], changes), base_url)


async def set_status(video_id: str, new_status: str, base_url: str) -> AdminVideoOut:
    doc = await _get(video_id)
    if doc["status"] == STATUS_UPLOADING:
        raise VideoNotReady()
    now = datetime.now(timezone.utc)
    changes: dict[str, Any] = {"status": new_status, "updated_at": now}
    if new_status == STATUS_PUBLISHED and doc["status"] != STATUS_PUBLISHED:
        changes["published_at"] = now
    return to_admin_out(await video_repo.update(doc["_id"], changes), base_url)


def _sniff_image(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def set_thumbnail(video_id: str, data: bytes, base_url: str) -> AdminVideoOut:
    doc = await _get(video_id)
    limit = settings.MAX_THUMBNAIL_SIZE_MB * MB
    if len(data) > limit:
        raise FileTooLarge(f"Thumbnails can be at most {settings.MAX_THUMBNAIL_SIZE_MB} MB.", details={"max_bytes": limit})
    content_type = _sniff_image(data)
    if not content_type:
        raise UnsupportedMediaType("Thumbnails must be JPEG, PNG or WebP images.", details={"allowed": sorted(ALLOWED_IMAGE_TYPES)})

    storage = get_storage()
    key = f"thumbnails/{doc['_id']}/{uuid.uuid4().hex}.{ALLOWED_IMAGE_TYPES[content_type]}"
    await storage.put(key, data, content_type)
    updated = await video_repo.update(
        doc["_id"],
        {"thumbnail": {"key": key, "size": len(data), "content_type": content_type},
         "updated_at": datetime.now(timezone.utc)},
    )
    if doc.get("thumbnail"):
        await storage.delete(doc["thumbnail"]["key"])
    return to_admin_out(updated, base_url)


async def remove_thumbnail(video_id: str, base_url: str) -> AdminVideoOut:
    doc = await _get(video_id)
    if not doc.get("thumbnail"):
        return to_admin_out(doc, base_url)
    updated = await video_repo.update(doc["_id"], {"updated_at": datetime.now(timezone.utc)}, unset_fields=["thumbnail"])
    await get_storage().delete(doc["thumbnail"]["key"])
    return to_admin_out(updated, base_url)


async def _delete_files(doc: dict) -> None:
    storage = get_storage()
    key = doc["file"]["key"]
    if doc["status"] == STATUS_UPLOADING:
        await storage.abort_upload(key)
    await storage.delete(key)
    if doc.get("thumbnail"):
        await storage.delete(doc["thumbnail"]["key"])
    await document_service.delete_for_video(doc)


async def delete_video(video_id: str) -> None:
    """Delete the record and its files. Works in any status (also cancels an upload)."""
    doc = await _get(video_id)
    await video_repo.delete(doc["_id"])
    await _delete_files(doc)
    logger.info("Video deleted: %s", video_id)


async def cleanup_expired_uploads() -> int:
    """Delete uploads that were never finished in time (record + partial file)."""
    expired = await video_repo.find_expired_uploads(datetime.now(timezone.utc))
    for doc in expired:
        await video_repo.delete(doc["_id"])
        try:
            await _delete_files(doc)
        except Exception as exc:  # noqa: BLE001 — cleanup must not break the caller
            logger.warning("Could not remove files of expired upload %s: %s", doc["_id"], exc)
    if expired:
        logger.info("Removed %d expired upload(s)", len(expired))
    return len(expired)


async def counts() -> dict[str, int]:
    by_status = await video_repo.count_by_status()
    return {s: by_status.get(s, 0) for s in (STATUS_UPLOADING, STATUS_DRAFT, STATUS_PUBLISHED)}
