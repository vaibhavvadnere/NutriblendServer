"""
services/video_service.py — Videos: chunked upload, details, publishing, playback links.

Upload protocol (resumable; every step is safe to retry):

    1. POST   /admin/videos                        details + file name/size/type
                                                   -> video (status "uploading") + chunk plan
    2. PUT    /admin/videos/{id}/upload/chunks/{n}  raw bytes of chunk n (any order, re-send OK)
       GET    /admin/videos/{id}/upload             which chunks are still missing (resume)
       POST   /admin/videos/{id}/upload/part-urls   signed links to PUT chunks straight
                                                   into R2 (browser uploads; chunk 0
                                                   always goes through the server)
    3. POST   /admin/videos/{id}/upload/complete    -> status "draft"

Steps 2–3 also accept an upload ticket (POST …/upload/ticket) instead of the
admin session, so a long upload outlives the 15-minute dashboard session.
    4. PATCH  /admin/videos/{id}/status             "published" -> visible in the app

Unfinished uploads expire after UPLOAD_SESSION_TTL_HOURS and are deleted,
files included (on startup and whenever a new upload starts).
"""

import asyncio
import difflib
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
    DocumentNotReady,
    DuplicateTitle,
    DuplicateVideo,
    FileTooLarge,
    InsufficientStorage,
    InvalidMediaFile,
    UnsupportedMediaType,
    UploadChecksumMismatch,
    UploadIncomplete,
    UploadInvalidChunk,
    UploadNotInProgress,
    VideoNotFound,
    VideoNotReady,
    VideoOptimizing,
)
from app.providers.storage import StorageError, get_staging_storage, get_storage
from app.providers.storage.base import ChecksumMismatch
from app.repositories import video_repo
from app.services import document_service, optimizer
from app.schemas.video import (
    ALLOWED_IMAGE_TYPES,
    ALLOWED_VIDEO_TYPES,
    AdminVideoOut,
    AdminVideoPage,
    BrokenVideo,
    CategoryCheck,
    DetailsCheck,
    OptimizationInfo,
    OptimizationStatus,
    TitleMatch,
    PartUrl,
    PartUrls,
    ReplacementInfo,
    StorageCheck,
    UploadProgress,
    UploadTicket,
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


def _store(doc: dict):
    """Where this video's file is: the staging folder while it waits to be optimized, otherwise the media storage."""
    return get_staging_storage() if doc["file"].get("staged") else get_storage()


# ── Output shaping ───────────────────────────────────────────────────────────

def _links(doc: dict, base_url: str) -> dict[str, Any]:
    if doc["status"] == STATUS_UPLOADING or doc["file"].get("staged"):
        return {}      # a file that is still being optimized is never played or linked
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


def _replacement_info(doc: dict) -> Optional[ReplacementInfo]:
    r = doc.get("replacement")
    if not r:
        return None
    return ReplacementInfo(
        video_id=r["id"], file_name=r.get("file_name"), file_size=r["file_size"], started_at=r["started_at"],
        optimization=r.get("optimization"),
    )


def _optimization_info(doc: dict) -> Optional[OptimizationInfo]:
    o = doc.get("optimization")
    if not o:
        return None
    return OptimizationInfo(
        state=o["state"], progress=o.get("progress") or 0.0, source_size=o.get("source_size"),
        output_size=o.get("output_size"), saved_percent=o.get("saved_percent"), mode=o.get("mode"),
        reason=o.get("reason"), error=o.get("error"), queued_at=o.get("queued_at"), finished_at=o.get("finished_at"),
    )


def optimization_wait_state(doc: dict) -> Optional[str]:
    """"queued" | "running" while the video's file is being optimized, "failed" when that failed, else None."""
    state = (doc.get("optimization") or {}).get("state")
    return state if state in ("queued", "running", "failed") else None


def optimization_status() -> OptimizationStatus:
    return OptimizationStatus(
        enabled=settings.VIDEO_OPTIMIZE_ENABLED, available=optimizer.available(), active=optimizer.active(),
        crf=settings.VIDEO_OPTIMIZE_CRF, preset=settings.VIDEO_OPTIMIZE_PRESET,
        skip_below_kbps=settings.VIDEO_OPTIMIZE_SKIP_BELOW_KBPS,
    )


def document_wait_state(doc: dict) -> Optional[str]:
    """Why this video's document isn't ready to be shown: "processing" | "failed" | "uploading"
    (a document is still on its way), or None when there is no document or it is ready."""
    d = doc.get("document")
    if d and d.get("status") != "ready":
        return d.get("status") or "processing"
    pending = doc.get("document_upload")
    if pending:
        expires = pending.get("expires_at")
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is None or expires > datetime.now(timezone.utc):
            return "uploading"
    return None


def is_visible_in_app(doc: dict) -> bool:
    """Published, and — if it has a document — only once that document is ready."""
    d = doc.get("document")
    return doc["status"] == STATUS_PUBLISHED and (not d or d.get("status") == "ready")


def to_admin_out(doc: dict, base_url: str) -> AdminVideoOut:
    return AdminVideoOut(
        **_common(doc),
        **_links(doc, base_url),
        status=doc["status"],
        original_file_name=doc["file"].get("original_name"),
        video_codec=doc["file"].get("video_codec"),
        sha256=doc["file"].get("sha256"),
        integrity_verified=bool(doc["file"].get("integrity_verified")),
        has_thumbnail=bool(doc.get("thumbnail")),
        visible_in_app=is_visible_in_app(doc),
        publish_when_ready=bool(doc.get("publish_when_ready")),
        replacement=_replacement_info(doc),
        optimization=_optimization_info(doc),
        replaces=str(doc["replaces"]) if doc.get("replaces") else None,
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
    if not doc or not is_visible_in_app(doc):
        raise VideoNotFound()
    return doc


async def get_published(video_id: str, base_url: str) -> VideoOut:
    doc = await video_repo.find_by_id(video_id)
    if not doc or not is_visible_in_app(doc):
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


# ── Title and category checks ────────────────────────────────────────────────

def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


async def _category_check(category: Optional[str]) -> CategoryCheck:
    """`canonical` = the spelling already in use when `category` differs only by capitals/spaces;
    `similar` = existing categories that look like a typo of it."""
    if not category or not category.strip():
        return CategoryCheck()
    existing = await video_repo.categories(published_only=False)
    wanted = _norm(category)
    for name in existing:
        if _norm(name) == wanted:
            return CategoryCheck(canonical=name, exists=True)
    by_key = {_norm(n): n for n in existing}
    close = difflib.get_close_matches(wanted, list(by_key), n=3, cutoff=0.75)
    return CategoryCheck(similar=[by_key[k] for k in close])


async def canonical_category(category: Optional[str]) -> Optional[str]:
    """Use the spelling an existing category already has ("recipes" → "Recipes"), so one category
    never splits into several in the app."""
    if not category:
        return category
    check = await _category_check(category)
    return check.canonical or category


async def _same_title(title: str, exclude_id: Optional[ObjectId] = None) -> list[TitleMatch]:
    return [TitleMatch(id=str(d["_id"]), title=d["title"], status=d["status"], created_at=d["created_at"])
            for d in await video_repo.find_by_title(title, exclude_id)]


async def check_details(title: Optional[str], category: Optional[str], exclude_id: Optional[str] = None) -> DetailsCheck:
    """What the dashboard shows while the admin types: same title elsewhere, category spelling."""
    exclude = ObjectId(exclude_id) if exclude_id and ObjectId.is_valid(exclude_id) else None
    same = await _same_title(title, exclude) if title else []
    return DetailsCheck(same_title=same, category=await _category_check(category))


async def _refuse_same_title(title: str, exclude_id: Optional[ObjectId] = None) -> None:
    same = await _same_title(title, exclude_id)
    if same:
        raise DuplicateTitle(
            f"A video called “{same[0].title}” already exists.",
            details={"videos": [m.model_dump(mode="json") for m in same]},
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
    sha256: Optional[str] = None,
    allow_duplicate: bool = False,
    duration_seconds: Optional[float] = None,
    video_codec: Optional[str] = None,
    replaces: Optional[ObjectId] = None,
    allow_duplicate_title: bool = True,       # the API passes False unless the admin confirmed
) -> AdminVideoOut:
    _validate_video_file(file_name, file_size, content_type)
    await cleanup_expired_uploads()
    if not allow_duplicate_title:
        await _refuse_same_title(title)
    category = await canonical_category(category)

    if sha256 and not allow_duplicate:
        existing = await video_repo.find_by_sha256(sha256)
        if existing:
            progress = _progress(existing)
            raise DuplicateVideo(
                f"This exact file is already uploaded as “{existing['title']}”"
                + (" (upload not finished — resume it instead)." if existing["status"] == STATUS_UPLOADING else "."),
                details={
                    "video_id": str(existing["_id"]),
                    "title": existing["title"],
                    "status": existing["status"],
                    "created_at": existing["created_at"].isoformat(),
                    "uploaded_fraction": round(progress.received_chunks / progress.total_chunks, 3) if progress else None,
                },
            )

    staged = optimizer.active()          # received on this server's disk, optimized, only then stored
    storage = get_staging_storage() if staged else get_storage()
    free = storage.free_bytes()
    if staged:
        # room for this upload and the encoded copy, on top of what other uploads still have to receive
        needed = file_size + file_size // 2 + await video_repo.staged_upload_bytes() + settings.VIDEO_STAGING_MARGIN_MB * MB
        if free is not None and free < needed:
            raise InsufficientStorage(
                "The server's staging disk is too full for this video right now. Try again after the "
                "videos being processed are finished.",
                details={"needed_bytes": needed, "free_bytes": free},
            )
    elif free is not None and free < file_size + _FREE_SPACE_MARGIN:
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
            **({"staged": True} if staged else {}),
            **({"sha256": sha256} if sha256 else {}),
            # Read by the admin's browser; ffprobe (when installed) overwrites them on completion.
            **({"duration_seconds": round(duration_seconds, 2)} if duration_seconds else {}),
            **({"video_codec": video_codec} if video_codec else {}),
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
        **({"replaces": replaces} if replaces else {}),
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


def _md5_b64(digest: bytes) -> str:
    import base64

    return base64.b64encode(digest).decode()


async def write_chunk(doc: dict, index: int, data: bytes, content_md5: Optional[str] = None) -> UploadProgress:
    """Store one chunk. `content_md5` is the base64 MD5 the client computed
    from its file (the Content-MD5 header). The server checks it, then makes
    the storage check it again, so every stored byte matches the client's file."""
    expected = expected_chunk_length(doc, index)
    if len(data) != expected:
        raise UploadInvalidChunk(
            f"Chunk {index} must be exactly {expected} bytes (got {len(data)}).",
            details={"expected_bytes": expected, "received_bytes": len(data)},
        )
    if index == 0 and data[4:8] != b"ftyp":
        # Every MP4 starts with an 'ftyp' box: a renamed .mov/.avi/.txt fails here.
        raise InvalidMediaFile("This file is not a valid MP4 video.")

    import hashlib

    digest = hashlib.md5(data).digest()
    if content_md5 is not None and content_md5.strip() != _md5_b64(digest):
        raise UploadChecksumMismatch(
            f"Chunk {index} was corrupted on the way (checksum mismatch). Send it again.",
            details={"chunk": index},
        )
    try:
        await _store(doc).write_chunk(
            doc["file"]["key"], index * doc["upload"]["chunk_size"], data, content_md5=_md5_b64(digest)
        )
    except ChecksumMismatch as exc:
        raise UploadChecksumMismatch(
            f"Chunk {index} was corrupted between the server and storage. Send it again.",
            details={"chunk": index},
        ) from exc
    updated = await video_repo.mark_chunk_received(doc["_id"], index, digest.hex())
    if not updated:
        raise UploadNotInProgress()
    return _progress(updated)


async def upload_progress(video_id: str) -> UploadProgress:
    doc = await _get(video_id)
    _uploading(doc)
    return _progress(await _reconcile(doc))


async def issue_ticket(video_id: str, admin: dict, purpose: str = "video") -> UploadTicket:
    """An upload ticket for this video: its file (while still uploading) or its document."""
    from app.core.security import UPLOAD_FOR_VIDEO, create_upload_ticket

    doc = await _get(video_id)
    if purpose == UPLOAD_FOR_VIDEO:
        _uploading(doc)
    ticket, expires_at = create_upload_ticket(str(doc["_id"]), str(admin["_id"]), purpose)
    return UploadTicket(video_id=str(doc["_id"]), ticket=ticket, expires_at=expires_at)


async def part_urls(video_id: str, parts: list[tuple[int, str]]) -> PartUrls:
    """Signed links to PUT chunks straight into storage. `parts` = (index,
    base64 MD5) as computed by the client: the link only accepts those exact
    bytes. The MD5s are recorded so the final check can prove what storage
    holds. Chunk 0 (MP4 check), local storage, or UPLOAD_DIRECT_TO_STORAGE=false
    -> url None: send that chunk through the server."""
    import base64

    doc = await get_upload_doc(video_id)
    storage = _store(doc)          # the staging folder has no direct links: every piece goes through the server
    ttl = settings.UPLOAD_PART_URL_TTL_SECONDS
    out: list[PartUrl] = []
    expected: dict[int, str] = {}
    for index, md5 in parts:
        expected_chunk_length(doc, index)  # range check
        url = None
        if index > 0 and settings.UPLOAD_DIRECT_TO_STORAGE:
            url = await storage.presign_part(doc["file"]["key"], index, md5, ttl)
        if url:
            expected[index] = base64.b64decode(md5).hex()
            out.append(PartUrl(index=index, url=url, headers={"Content-MD5": md5}))
        else:
            out.append(PartUrl(index=index))
    if expected and not await video_repo.expect_chunks(doc["_id"], expected):
        raise UploadNotInProgress()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl) if expected else None
    return PartUrls(parts=out, expires_at=expires_at)


async def _reconcile(doc: dict) -> dict:
    """Chunks the browser PUT straight into storage: mark as received the ones
    storage holds with the announced size and MD5. Storage is the truth, so a
    resumed upload knows exactly what is still missing."""
    up = doc.get("upload") or {}
    if doc.get("status") != STATUS_UPLOADING or up.get("state") != "receiving":
        return doc
    received = set(up.get("received", []))
    pending = {int(i): h for i, h in (up.get("md5") or {}).items() if int(i) not in received}
    if not pending:
        return doc
    try:
        held = await _store(doc).part_checksums(doc["file"]["key"], up["chunk_size"])
    except StorageError as exc:
        logger.warning("Could not list stored parts of video %s: %s", doc["_id"], exc)
        return doc
    if not held:
        return doc
    arrived = [
        i for i, want in pending.items()
        if i in held and held[i][0] == expected_chunk_length(doc, i) and held[i][1] == want
    ]
    if not arrived:
        return doc
    return await video_repo.mark_chunks_received(doc["_id"], sorted(arrived)) or doc


async def complete_upload(video_id: str, base_url: str) -> AdminVideoOut:
    doc = await _get(video_id)
    _uploading(doc)
    doc = await _reconcile(doc)
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

    storage = _store(claimed)
    key = claimed["file"]["key"]
    try:
        head = await storage.read_upload_head(key, 12)
        if head[4:8] != b"ftyp":
            raise InvalidMediaFile("This file is not a valid MP4 video.")
        verified = await _verify_stored_chunks(claimed, storage, key)
        await storage.complete_upload(key)
    except Exception:
        await video_repo.release_assembly(doc["_id"])
        raise

    target = storage.local_path(key)
    if target is None:
        try:
            target = await storage.presigned_url(key, 600)
        except StorageError:  # probing is optional; never fail a finished upload
            target = None
    probe = await _probe(target)
    now = datetime.now(timezone.utc)
    changes: dict[str, Any] = {
        "status": STATUS_DRAFT,
        "uploaded_at": now,
        "updated_at": now,
        "file.integrity_verified": verified,
        **{f"file.{k}": v for k, v in probe.items()},
    }
    staged = bool(claimed["file"].get("staged"))
    if staged:
        changes["optimization"] = {"state": "queued", "progress": 0.0, "queued_at": now,
                                   "source_size": claimed["file"]["size"]}
    updated = await video_repo.update(doc["_id"], changes, unset_fields=["upload"])
    logger.info("Upload complete: video=%s staged=%s probe=%s", video_id, staged, probe or "unavailable")
    if staged:
        # optimized first; a replacement is swapped in by the optimizer when its file is ready
        await set_replacement_state(updated, "queued")
        return to_admin_out(updated, base_url)
    if updated.get("replaces"):
        return await _swap_replacement(updated, base_url)
    return to_admin_out(updated, base_url)


# ── Replacing a video's file ─────────────────────────────────────────────────
# The new file is uploaded as an unfinished video record of its own (`replaces` = the video),
# through exactly the same upload machinery. The video keeps playing its old file until the
# new one is complete and verified; then the file is swapped into the video and the old one
# deleted. Title, thumbnail, document, category, order and publish status stay as they are.

async def start_replacement(
    video_id: str, admin: dict, *, file_name: str, file_size: int, content_type: str, base_url: str,
    sha256: Optional[str] = None, allow_duplicate: bool = False,
    duration_seconds: Optional[float] = None, video_codec: Optional[str] = None,
) -> AdminVideoOut:
    """Begin uploading a new file for this video. Returns the unfinished upload (an `uploading`
    record: send its chunks as for any upload; completing it swaps the file in and returns the video)."""
    target = await _get(video_id)
    if target["status"] == STATUS_UPLOADING or target.get("replaces"):
        raise VideoNotReady("Finish uploading this video before replacing its file.")
    if optimization_wait_state(target):
        raise VideoNotReady("This video's file is still being optimized. Replace it once that is finished.")
    pending = target.get("replacement")
    if pending:
        staging = await video_repo.find_by_id(pending["id"])
        if staging and staging["status"] == STATUS_UPLOADING:
            same = bool(sha256) and staging["file"].get("sha256") == sha256 and staging["file"]["size"] == file_size
            try:
                _uploading(staging)
                alive = True
            except UploadNotInProgress:
                alive = False
            if same and alive:
                return to_admin_out(staging, base_url)          # the same file again: carry on with that upload
            await _discard_replacement(target, staging)         # another file chosen: the unfinished one is dropped
        else:
            await video_repo.update(target["_id"], {}, unset_fields=["replacement"])
    staging = await start_upload(
        admin, title=f"{target['title']} (new file)", description=None, category=None, sort_order=0,
        file_name=file_name, file_size=file_size, content_type=content_type, base_url=base_url,
        sha256=sha256, allow_duplicate=allow_duplicate, duration_seconds=duration_seconds,
        video_codec=video_codec, replaces=target["_id"],
    )
    await video_repo.update(target["_id"], {"replacement": {
        "id": staging.id, "file_name": file_name[-255:], "file_size": file_size,
        "started_at": datetime.now(timezone.utc),
    }})
    logger.info("Replacement started: video=%s upload=%s size=%d", video_id, staging.id, file_size)
    return staging


async def _discard_replacement(target: dict, staging: dict) -> None:
    await video_repo.delete(staging["_id"])
    await video_repo.update(target["_id"], {}, unset_fields=["replacement"])
    try:
        await _delete_files(staging)
    except Exception as exc:  # noqa: BLE001 — the record is gone; a leftover partial file is only wasted space
        logger.warning("Could not remove files of discarded replacement %s: %s", staging["_id"], exc)


async def set_replacement_state(staging: dict, state: Optional[str]) -> None:
    """Mirror the optimization state of an unfinished replacement onto the video it will replace."""
    from app.core.database import videos_collection

    if staging.get("replaces"):
        op = {"$set": {"replacement.optimization": state}} if state else {"$unset": {"replacement.optimization": ""}}
        await videos_collection.update_one({"_id": staging["replaces"], "replacement.id": str(staging["_id"])}, op)


async def swap_replacement(staging: dict, base_url: str) -> AdminVideoOut:
    return await _swap_replacement(staging, base_url)


async def _swap_replacement(staging: dict, base_url: str) -> AdminVideoOut:
    """The new file is complete and verified: make it the video's file, then delete the old one."""
    target = await video_repo.find_by_id(str(staging["replaces"]))
    if not target:
        await video_repo.delete(staging["_id"])
        await _delete_files(staging)
        raise VideoNotFound("The video this file was meant to replace no longer exists.")
    old_key = target["file"]["key"]
    now = datetime.now(timezone.utc)
    changes: dict[str, Any] = {"file": dict(staging["file"]), "uploaded_at": now, "updated_at": now}
    drop = ["replacement"]
    if staging.get("optimization"):
        changes["optimization"] = staging["optimization"]       # what the new file went through
    else:
        drop.append("optimization")
    updated = await video_repo.update(target["_id"], changes, unset_fields=drop)
    await video_repo.delete(staging["_id"])
    if old_key != staging["file"]["key"]:
        try:
            await get_storage().delete(old_key)
        except Exception as exc:  # noqa: BLE001 — the swap is done; a leftover old file is only wasted space
            logger.warning("Could not delete the old file %s of video %s: %s", old_key, target["_id"], exc)
    logger.info("Video file replaced: video=%s new=%s old=%s", target["_id"], staging["file"]["key"], old_key)
    return to_admin_out(updated, base_url)


async def _forget_replacement(staging: dict) -> None:
    """An unfinished replacement is gone (deleted or expired): the video no longer waits for it."""
    from app.core.database import videos_collection

    if staging.get("replaces"):
        await videos_collection.update_one(
            {"_id": staging["replaces"], "replacement.id": str(staging["_id"])}, {"$unset": {"replacement": ""}}
        )


async def _verify_stored_chunks(doc: dict, storage, key: str) -> bool:
    """Before joining the chunks, compare what the storage holds with what was
    received: every chunk must have the right size and (when known) the MD5
    recorded on arrival; with a declared SHA-256, the whole file must match too
    (where the storage can read it back). Bad chunks are forgotten so the
    client re-sends them (409 UPLOAD_INCOMPLETE listing them). Returns True when
    every chunk's content was verified."""
    up = doc["upload"]
    size, total, chunk = doc["file"]["size"], up["total_chunks"], up["chunk_size"]
    recorded: dict[str, str] = up.get("md5") or {}
    held = await storage.part_checksums(key, chunk)

    bad: list[int] = []
    content_checked = held is not None
    if held is not None:
        for i in range(total):
            want_size = chunk if i < total - 1 else size - chunk * (total - 1)
            got = held.get(i)
            if got is None or got[0] != want_size:
                bad.append(i)
                continue
            want_md5 = recorded.get(str(i))
            if got[1] and want_md5:
                if got[1] != want_md5:
                    bad.append(i)
            else:
                content_checked = False
    if bad:
        await video_repo.forget_chunks(doc["_id"], bad)
        raise UploadIncomplete(
            f"{len(bad)} chunk(s) failed verification in storage and must be sent again.",
            details={"missing_chunks": bad[:50], "reason": "verification_failed"},
        )

    declared = doc["file"].get("sha256")
    whole = await storage.upload_sha256(key) if declared else None
    if declared and whole and whole != declared:
        # Chunks matched their own MD5s but the file isn't the declared one:
        # the client sent a different file. Start the bytes over.
        await video_repo.forget_chunks(doc["_id"], list(range(total)))
        raise UploadChecksumMismatch(
            "The uploaded bytes don't match the file's SHA-256. The upload was reset — send the file again.",
            details={"expected_sha256": declared},
        )
    all_md5_recorded = all(str(i) in recorded for i in range(total))
    return bool(all_md5_recorded and (content_checked or (declared and whole == declared)))


async def _probe(path) -> dict[str, Any]:
    """Duration and video codec via ffprobe, when it is installed. Optional.
    `path` is a local file or (cloud storage) a short-lived https URL: ffprobe
    only reads the parts of the file it needs."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or path is None:
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            raise
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

async def update_details(
    video_id: str, changes: dict[str, Any], base_url: str, allow_duplicate_title: bool = True
) -> AdminVideoOut:
    doc = await _get(video_id)
    if not changes:
        return to_admin_out(doc, base_url)
    if changes.get("title") and not allow_duplicate_title and _norm(changes["title"]) != _norm(doc["title"]):
        await _refuse_same_title(changes["title"], doc["_id"])
    if changes.get("category"):
        changes["category"] = await canonical_category(changes["category"])
    changes["updated_at"] = datetime.now(timezone.utc)
    return to_admin_out(await video_repo.update(doc["_id"], changes), base_url)


# ── Storage consistency ──────────────────────────────────────────────────────

async def missing_files(doc: dict) -> list[str]:
    """Which files of this record are not in the current storage. Raises
    StorageError if the storage can't be reached (so "missing" is never a guess)."""
    storage = get_storage()
    missing: list[str] = []
    if doc["status"] != STATUS_UPLOADING and not await _store(doc).exists(doc["file"]["key"]):
        missing.append("video")
    if doc.get("thumbnail") and not await storage.exists(doc["thumbnail"]["key"]):
        missing.append("thumbnail")
    d = doc.get("document")
    if d and not await storage.exists(d["key"]):
        missing.append("document")
    return missing


async def get_admin_checked(video_id: str, base_url: str) -> AdminVideoOut:
    """One video plus its missing files (for the dashboard's video page)."""
    doc = await _reconcile(await _get(video_id))
    out = to_admin_out(doc, base_url)
    try:
        out.missing_files = await missing_files(doc)
    except StorageError as exc:
        logger.warning("Could not check files of video %s: %s", video_id, exc)
    return out


async def storage_check() -> StorageCheck:
    """Every video record whose files are missing in the current storage —
    e.g. records created with STORAGE_PROVIDER=local before moving to R2."""
    docs = await video_repo.all_for_check()
    sem = asyncio.Semaphore(8)
    broken: list[BrokenVideo] = []
    errors = 0

    async def check(doc: dict) -> None:
        nonlocal errors
        async with sem:
            try:
                missing = await missing_files(doc)
            except StorageError as exc:
                errors += 1
                logger.warning("Storage check failed for %s: %s", doc["_id"], exc)
                return
        if missing:
            broken.append(BrokenVideo(id=str(doc["_id"]), title=doc["title"], status=doc["status"],
                                      created_at=doc["created_at"], missing_files=missing))

    await asyncio.gather(*(check(d) for d in docs))
    broken.sort(key=lambda b: b.created_at)
    return StorageCheck(storage=get_storage().name, checked=len(docs), broken=broken, errors=errors)


async def set_status(video_id: str, new_status: str, base_url: str, when_ready: bool = False) -> AdminVideoOut:
    """draft / published. Publishing waits for the document: while it is being prepared, either
    409 DOCUMENT_NOT_READY, or (when_ready) the video stays a draft and goes live by itself the
    moment the document is ready. A failed document blocks publishing until it is retried or removed."""
    doc = await _get(video_id)
    if doc["status"] == STATUS_UPLOADING:
        raise VideoNotReady()
    now = datetime.now(timezone.utc)
    if new_status == STATUS_PUBLISHED and doc["status"] != STATUS_PUBLISHED:
        try:
            if "video" in await missing_files(doc):
                raise VideoNotReady(
                    "This video's file is missing from storage, so it can't be published. "
                    "Delete it and upload the video again."
                )
        except StorageError as exc:
            logger.warning("Could not verify file before publishing %s: %s", video_id, exc)
        opt = optimization_wait_state(doc)
        if opt == "failed":
            raise VideoOptimizing(
                "This video couldn't be optimized, so it can't be published. Retry the optimization, "
                "or delete the video and upload it again.",
                details={"optimization_state": "failed"},
            )
        wait = document_wait_state(doc)
        if wait == "failed":
            raise DocumentNotReady(
                "This video's document couldn't be prepared, so the video can't be published. "
                "Retry the document or remove it, then publish.",
                details={"document_status": wait},
            )
        if opt or wait:
            if not when_ready and opt:
                raise VideoOptimizing(
                    "This video is still being optimized. Publish once that is finished — or choose "
                    "“publish when ready” and it goes live by itself.",
                    details={"optimization_state": opt, "can_publish_when_ready": True},
                )
            if not when_ready:
                raise DocumentNotReady(
                    "This video's document is still being prepared. Publish once it is ready — or choose "
                    "“publish when ready” and it goes live by itself.",
                    details={"document_status": wait, "can_publish_when_ready": True},
                )
            updated = await video_repo.update(doc["_id"], {"publish_when_ready": True, "updated_at": now})
            # the document may have become ready between the check and the flag: don't leave it waiting forever
            updated = await video_repo.publish_if_waiting(doc["_id"]) or updated
            logger.info("Video %s will be published when ready (optimization %s, document %s)", video_id, opt, wait)
            return to_admin_out(updated, base_url)
    changes: dict[str, Any] = {"status": new_status, "updated_at": now}
    if new_status == STATUS_PUBLISHED and doc["status"] != STATUS_PUBLISHED:
        changes["published_at"] = now
    # publishing or un-publishing by hand ends any "publish when ready" wait
    return to_admin_out(await video_repo.update(doc["_id"], changes, unset_fields=["publish_when_ready"]), base_url)


async def retry_optimization(video_id: str, base_url: str) -> AdminVideoOut:
    """Queue a failed optimization again (the upload is still in the staging folder)."""
    doc = await _get(video_id)
    if (doc.get("optimization") or {}).get("state") != "failed":
        raise VideoNotReady("Only a failed optimization can be retried.")
    if not await get_staging_storage().exists(doc["file"]["key"]):
        raise VideoNotReady("The uploaded file is no longer on the server. Delete the video and upload it again.")
    now = datetime.now(timezone.utc)
    updated = await video_repo.update(
        doc["_id"],
        {"optimization.state": "queued", "optimization.progress": 0.0, "optimization.queued_at": now, "updated_at": now},
        unset_fields=["optimization.error", "optimization.finished_at"],
    )
    await set_replacement_state(updated, "queued")
    return to_admin_out(updated, base_url)


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
        await _store(doc).abort_upload(key)
    keep_file = False
    if doc.get("replaces"):
        # a finished replacement whose file already became the video's file must not take it along
        target = await video_repo.find_by_id(str(doc["replaces"]))
        keep_file = bool(target and target["file"]["key"] == key)
    if not keep_file:
        await _store(doc).delete(key)
    if doc.get("thumbnail"):
        await storage.delete(doc["thumbnail"]["key"])
    await document_service.delete_for_video(doc)


async def delete_video(video_id: str) -> None:
    """Delete the record and its files. Works in any status (also cancels an upload)."""
    doc = await _get(video_id)
    await video_repo.delete(doc["_id"])
    await _delete_files(doc)
    await _forget_replacement(doc)                      # deleting an unfinished replacement cancels it
    pending = doc.get("replacement")
    if pending:                                         # deleting a video drops its unfinished replacement too
        staging = await video_repo.find_by_id(pending["id"])
        if staging:
            await video_repo.delete(staging["_id"])
            try:
                await _delete_files(staging)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not remove the unfinished replacement %s: %s", staging["_id"], exc)
    logger.info("Video deleted: %s", video_id)


async def cleanup_expired_uploads() -> int:
    """Delete uploads that were never finished in time (record + partial file)."""
    expired = await video_repo.find_expired_uploads(datetime.now(timezone.utc))
    for doc in expired:
        await video_repo.delete(doc["_id"])
        await _forget_replacement(doc)
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
