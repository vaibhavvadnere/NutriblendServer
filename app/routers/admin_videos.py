"""
routers/admin_videos.py — Video management for the dashboard (admin session).

Upload a video in three steps (resumable — see services/video_service.py):

    POST /admin/videos                          -> video + chunk plan
    PUT  /admin/videos/{id}/upload/chunks/{n}   raw bytes (application/octet-stream)
    POST /admin/videos/{id}/upload/complete     -> status "draft"

The dashboard's in-browser upload box asks for an upload ticket
(POST …/upload/ticket) and signed links (POST …/upload/part-urls) to send
chunks straight into R2; the upload endpoints accept the ticket in place of
the admin session (see `upload_router`).

then PATCH /admin/videos/{id}/status {"status": "published"} to show it in the app.
"""

from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, Path, Query, Request, UploadFile, status

from app.core.config import settings
from app.core.deps import public_base_url, require_admin, require_document_upload_access, require_upload_access
from app.core.exceptions import FileTooLarge, UploadInvalidChunk
from app.schemas.common import ERROR_RESPONSES, ApiResponse, EmptyData, ok
from app.schemas.video import (
    AdminVideoOut,
    AdminVideoPage,
    DocumentPages,
    DocumentUploadLink,
    DocumentUploadRequest,
    PartUrlRequest,
    PartUrls,
    StorageCheck,
    CategoryList,
    DetailsCheck,
    OptimizationStatus,
    UploadProgress,
    UploadTicket,
    VideoCreateRequest,
    VideoReplaceRequest,
    VideoStatusRequest,
    VideoUpdateRequest,
)
from app.services import document_service, video_service

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/admin/videos",
    tags=["admin videos"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_admin)],
)

# Sending a document: admin session OR a document upload ticket for that video.
document_router = APIRouter(
    prefix=f"{settings.API_PREFIX}/admin/videos",
    tags=["admin video documents"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_document_upload_access)],
)

# Sending chunks / progress / complete: admin session OR an upload ticket for
# that one video (the browser upload box keeps going after the session expires).
upload_router = APIRouter(
    prefix=f"{settings.API_PREFIX}/admin/videos",
    tags=["admin video upload"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_upload_access)],
)


@router.post("", response_model=ApiResponse[AdminVideoOut], status_code=status.HTTP_201_CREATED)
async def create_video(payload: VideoCreateRequest, request: Request, admin: dict = Depends(require_admin)):
    """
    Step 1: create the video (status `uploading`) and get the chunk plan
    (`upload.chunk_size`, `upload.total_chunks`). Only MP4 (`video/mp4`).

    - `413 FILE_TOO_LARGE` (over MAX_VIDEO_SIZE_MB) · `415 UNSUPPORTED_MEDIA_TYPE`
    - `409 DUPLICATE_VIDEO` — a video with the same `sha256` exists (`details` names it);
      send `allow_duplicate: true` to upload anyway.
    - `409 DUPLICATE_TITLE` — another video has this title (`details.videos`); send
      `allow_duplicate_title: true` to create it anyway.
    - A category that differs from an existing one only by capitals/spaces takes the existing spelling.
    - `507 INSUFFICIENT_STORAGE` — not enough disk space on the server.
    """
    video = await video_service.start_upload(
        admin,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        sort_order=payload.sort_order,
        file_name=payload.file_name,
        file_size=payload.file_size,
        content_type=payload.content_type,
        base_url=public_base_url(request),
        sha256=payload.sha256,
        allow_duplicate=payload.allow_duplicate,
        duration_seconds=payload.duration_seconds,
        video_codec=payload.video_codec,
        allow_duplicate_title=payload.allow_duplicate_title,
    )
    return ok(video, "Upload started. Send the chunks, then call complete.")


@router.post("/{video_id}/replace-file", response_model=ApiResponse[AdminVideoOut], status_code=status.HTTP_201_CREATED)
async def replace_video_file(video_id: str, payload: VideoReplaceRequest, request: Request, admin: dict = Depends(require_admin)):
    """
    Replace the video's file with a new one, keeping the record, links, order, thumbnail, document and
    publish status. Returns an **unfinished upload** (`status: uploading`, `replaces` = this video): send it
    like any upload (ticket, chunks / part-urls) and call its `upload/complete` — that swaps the new
    file in and returns the updated video. Until then the video keeps playing its old file; the old file
    is deleted after the swap. The video shows the pending upload in `replacement`.

    Sending the same file again resumes the unfinished upload; another file discards it.
    `409 DUPLICATE_VIDEO` if this exact file exists (`allow_duplicate` overrides), `409 VIDEO_NOT_READY` while
    the video itself is still uploading.
    """
    out = await video_service.start_replacement(
        video_id, admin, file_name=payload.file_name, file_size=payload.file_size, content_type=payload.content_type,
        base_url=public_base_url(request), sha256=payload.sha256, allow_duplicate=payload.allow_duplicate,
        duration_seconds=payload.duration_seconds, video_codec=payload.video_codec,
    )
    return ok(out, "Upload of the new file started. Send the chunks, then call complete.")


@router.post("/{video_id}/upload/ticket", response_model=ApiResponse[UploadTicket])
async def upload_ticket(video_id: str, admin: dict = Depends(require_admin)):
    """
    A ticket for the upload endpoints of this one video, valid for
    UPLOAD_TICKET_TTL_HOURS. Send it as `X-Upload-Ticket` (instead of the
    admin's Bearer token) so a long upload isn't cut off when the 15-minute
    dashboard session ends.
    """
    return ok(await video_service.issue_ticket(video_id, admin), "Upload ticket issued")


@upload_router.post("/{video_id}/upload/part-urls", response_model=ApiResponse[PartUrls])
async def part_urls(video_id: str, payload: PartUrlRequest):
    """
    Signed links to PUT chunks straight into storage (R2), up to 64 at a time.
    Send each chunk's base64 MD5; PUT the chunk to `url` with exactly the
    returned `headers` (Content-MD5). R2 rejects any other bytes.

    `url: null` (always for chunk 0, and for local storage) = send that chunk
    through `PUT …/upload/chunks/{index}` instead. Chunks sent directly show
    up in `GET …/upload` once storage holds them.
    """
    result = await video_service.part_urls(video_id, [(p.index, p.md5) for p in payload.parts])
    direct = sum(1 for p in result.parts if p.url)
    return ok(result, f"{direct} direct link(s), {len(result.parts) - direct} via server")


@upload_router.put("/{video_id}/upload/chunks/{index}", response_model=ApiResponse[UploadProgress])
async def upload_chunk(request: Request, video_id: str, index: int = Path(ge=0)):
    """
    Step 2: send chunk `index` (0-based) as the raw request body
    (`Content-Type: application/octet-stream`). Every chunk is exactly
    `chunk_size` bytes except the last. Chunks may arrive in any order and
    may be re-sent (e.g. after a network error).

    Chunk 0 must start like an MP4 file, otherwise `422 INVALID_MEDIA_FILE`.

    Send `Content-MD5: <base64 MD5 of the chunk>` so the server (and the
    storage) can prove the bytes arrived intact; a mismatch answers
    `400 UPLOAD_CHECKSUM_MISMATCH` — just send the chunk again.
    """
    doc = await video_service.get_upload_doc(video_id)
    expected = video_service.expected_chunk_length(doc, index)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > expected:
        raise FileTooLarge(f"Chunk {index} must be exactly {expected} bytes.", details={"expected_bytes": expected})

    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > expected:
            raise UploadInvalidChunk(f"Chunk {index} must be exactly {expected} bytes.", details={"expected_bytes": expected})

    progress = await video_service.write_chunk(doc, index, bytes(body), request.headers.get("content-md5"))
    return ok(progress, f"Chunk {index} received ({progress.received_chunks}/{progress.total_chunks})")


@upload_router.get("/{video_id}/upload", response_model=ApiResponse[UploadProgress])
async def upload_status(video_id: str):
    """Which chunks have arrived and which are missing — use it to resume an interrupted upload."""
    return ok(await video_service.upload_progress(video_id), "Upload progress")


@upload_router.post("/{video_id}/upload/complete", response_model=ApiResponse[AdminVideoOut])
async def complete_upload(video_id: str, request: Request):
    """
    Step 3: finish the upload. Every chunk must have arrived
    (`409 UPLOAD_INCOMPLETE` lists the missing ones). The video becomes `draft`.
    """
    return ok(await video_service.complete_upload(video_id, public_base_url(request)), "Upload complete")


@router.get("", response_model=ApiResponse[AdminVideoPage])
async def list_videos(
    request: Request,
    q: Optional[str] = Query(default=None, max_length=60, description="Part of the title, description or category"),
    status: Optional[Literal["uploading", "draft", "published"]] = Query(default=None),
    category: Optional[str] = Query(default=None, max_length=40),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=video_service.MAX_PAGE_SIZE),
):
    """All videos (drafts and uploads in progress included), newest first."""
    return ok(
        await video_service.list_admin(q, status, category, page, page_size, public_base_url(request)),
        "Videos fetched successfully",
    )


@router.get("/storage-check", response_model=ApiResponse[StorageCheck])
async def storage_check():
    """
    Find videos whose files are missing from the current storage (for example
    records created before STORAGE_PROVIDER changed from local to r2). Delete
    them with DELETE /admin/videos/{id} — that works even when files are gone.
    """
    result = await video_service.storage_check()
    return ok(result, f"{len(result.broken)} of {result.checked} videos have missing files")


@router.get("/categories", response_model=ApiResponse[CategoryList])
async def list_categories():
    """Every category used by any video (for the dashboard's category picker)."""
    return ok(CategoryList(categories=await video_service.categories(published_only=False)), "Categories fetched")


@router.get("/optimization", response_model=ApiResponse[OptimizationStatus])
async def optimization_status():
    """Whether new videos are optimized (re-encoded smaller on the server) and with which settings."""
    return ok(video_service.optimization_status(), "Optimization settings")


@router.post("/{video_id}/optimization/retry", response_model=ApiResponse[AdminVideoOut])
async def retry_optimization(video_id: str, request: Request):
    """Queue a failed optimization again. `409 VIDEO_NOT_READY` unless it failed."""
    return ok(await video_service.retry_optimization(video_id, public_base_url(request)), "Optimization queued again")


@router.get("/check-details", response_model=ApiResponse[DetailsCheck])
async def check_details(title: Optional[str] = None, category: Optional[str] = None, exclude_id: Optional[str] = None):
    """Before saving: other videos with the same title, and whether the category is spelled like an
    existing one (`canonical`) or looks like a typo of one (`similar`). Nothing is changed."""
    return ok(await video_service.check_details(title, category, exclude_id), "Checked")


@router.get("/{video_id}", response_model=ApiResponse[AdminVideoOut])
async def get_video(video_id: str, request: Request):
    """One video, with a playback link (once uploaded) for previewing."""
    return ok(await video_service.get_admin_checked(video_id, public_base_url(request)), "Video fetched successfully")


@router.patch("/{video_id}", response_model=ApiResponse[AdminVideoOut])
async def update_video(video_id: str, payload: VideoUpdateRequest, request: Request):
    """Edit title, description, category or display order. Only the fields you send change.
    `409 DUPLICATE_TITLE` if another video has the new title (`allow_duplicate_title: true` overrides)."""
    changes = payload.model_dump(include=payload.model_fields_set - {"allow_duplicate_title"})
    if "title" in changes and changes["title"] is None:
        changes.pop("title")  # title can be changed but not removed
    if "sort_order" in changes and changes["sort_order"] is None:
        changes.pop("sort_order")
    return ok(
        await video_service.update_details(
            video_id, changes, public_base_url(request), allow_duplicate_title=payload.allow_duplicate_title),
        "Video updated",
    )


@router.patch("/{video_id}/status", response_model=ApiResponse[AdminVideoOut])
async def set_video_status(video_id: str, payload: VideoStatusRequest, request: Request):
    """`published` shows the video in the app; `draft` hides it. `409 VIDEO_NOT_READY` while uploading.

    A video with a document is only shown once the document is ready: while it is being prepared
    publishing answers `409 DOCUMENT_NOT_READY` (`details.can_publish_when_ready`) — or send
    `"when_ready": true` and the video stays a draft (`publish_when_ready`) and is published
    automatically when the document is ready. A failed document blocks publishing (retry or remove it).
    """
    video = await video_service.set_status(video_id, payload.status, public_base_url(request), payload.when_ready)
    return ok(video, f"Video is now {video.status}")


@router.put("/{video_id}/thumbnail", response_model=ApiResponse[AdminVideoOut])
async def upload_thumbnail(video_id: str, request: Request, file: UploadFile = File(...)):
    """Set (or replace) the cover image: JPEG, PNG or WebP, up to MAX_THUMBNAIL_SIZE_MB."""
    limit = settings.MAX_THUMBNAIL_SIZE_MB * 1024 * 1024
    data = await file.read(limit + 1)
    return ok(await video_service.set_thumbnail(video_id, data, public_base_url(request)), "Thumbnail saved")


@router.delete("/{video_id}/thumbnail", response_model=ApiResponse[AdminVideoOut])
async def delete_thumbnail(video_id: str, request: Request):
    return ok(await video_service.remove_thumbnail(video_id, public_base_url(request)), "Thumbnail removed")


@router.post("/{video_id}/document/ticket", response_model=ApiResponse[UploadTicket])
async def document_ticket(video_id: str, admin: dict = Depends(require_admin)):
    """A ticket for the document upload endpoints of this one video (the
    dashboard's in-browser upload box). Send it as `X-Upload-Ticket`."""
    from app.core.security import UPLOAD_FOR_DOCUMENT

    return ok(await video_service.issue_ticket(video_id, admin, UPLOAD_FOR_DOCUMENT), "Upload ticket issued")


@document_router.post("/{video_id}/document/upload", response_model=ApiResponse[DocumentUploadLink])
async def start_document_upload(video_id: str, payload: DocumentUploadRequest):
    """
    Start sending a document **straight to storage** (R2) from the browser:
    PUT the whole file to `url` with exactly `headers`, then call
    `POST …/document/upload/{upload_id}/complete`.

    `url: null` (local storage, or direct uploads off) = send it with
    `PUT /admin/videos/{id}/document` instead.
    """
    link = await document_service.start_direct(video_id, payload.file_name, payload.size, payload.sha256)
    return ok(link, "Send the document" + (" straight to storage" if link.url else " through the server"))


@document_router.post("/{video_id}/document/upload/{upload_id}/complete", response_model=ApiResponse[AdminVideoOut])
async def complete_document_upload(video_id: str, upload_id: str, request: Request, background: BackgroundTasks):
    """
    The document is in storage: it is checked (size, file type; SHA-256 while
    preparing) and replaces the previous document. Word/PowerPoint are then
    converted in the background (`admin_document.status` processing → ready).

    `409 UPLOAD_INCOMPLETE` — the file didn't arrive whole: send it again.
    """
    await document_service.finish_direct(video_id, upload_id)
    background.add_task(document_service.prepare, video_id)
    return ok(await video_service.get_admin(video_id, public_base_url(request)), "Document uploaded")


@document_router.put("/{video_id}/document", response_model=ApiResponse[AdminVideoOut])
async def upload_document(
    video_id: str,
    request: Request,
    background: BackgroundTasks,
    x_file_name: str = Header(..., alias="X-File-Name", description="Original file name, URL-encoded"),
    x_file_sha256: Optional[str] = Header(default=None, alias="X-File-SHA256",
                                          description="SHA-256 of the file (hex); checked before storing"),
):
    """
    Attach the video's document **through the server** (replaces any existing
    one): **PDF, Word (.docx) or PowerPoint (.pptx)**, sent as the raw request
    body. The dashboard normally sends documents straight to storage instead
    (`POST …/document/upload`); this is the fallback and the local-storage path.

    Documents are **view-only**: the original is never served. Word/PowerPoint
    are converted to pages in the background — `admin_document.status` goes
    `processing` → `ready` (or `failed` with `error`).
    """
    declared = request.headers.get("content-length")
    await document_service.upload(
        video_id, x_file_name, request.stream(), int(declared) if declared and declared.isdigit() else None,
        sha256=x_file_sha256,
    )
    background.add_task(document_service.prepare, video_id)
    return ok(await video_service.get_admin(video_id, public_base_url(request)), "Document uploaded")


@router.delete("/{video_id}/document", response_model=ApiResponse[AdminVideoOut])
async def delete_document(video_id: str, request: Request):
    """Remove the document (and its rendered pages)."""
    await document_service.remove(video_id)
    return ok(await video_service.get_admin(video_id, public_base_url(request)), "Document removed")


@router.post("/{video_id}/document/retry", response_model=ApiResponse[AdminVideoOut])
async def retry_document(video_id: str, request: Request, background: BackgroundTasks):
    """Try preparing the document again (e.g. after installing LibreOffice)."""
    await document_service.retry(video_id)
    background.add_task(document_service.prepare, video_id)
    return ok(await video_service.get_admin(video_id, public_base_url(request)), "Preparing document")


@router.get("/{video_id}/document/pages", response_model=ApiResponse[DocumentPages])
async def document_pages(video_id: str, request: Request):
    """Signed links to the document's page images (preview in the dashboard)."""
    from app.repositories import video_repo
    from app.core.exceptions import VideoNotFound

    video = await video_repo.find_by_id(video_id)
    if not video:
        raise VideoNotFound()
    return ok(document_service.pages_for(video, public_base_url(request)), "Document pages")


@router.delete("/{video_id}", response_model=ApiResponse[EmptyData])
async def delete_video(video_id: str):
    """Delete the video and its files (also cancels an upload in progress). Cannot be undone."""
    await video_service.delete_video(video_id)
    return ok(EmptyData(), "Video deleted")
