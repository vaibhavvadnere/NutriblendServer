"""
routers/admin_videos.py — Video management for the dashboard (admin session).

Upload a video in three steps (resumable — see services/video_service.py):

    POST /admin/videos                          -> video + chunk plan
    PUT  /admin/videos/{id}/upload/chunks/{n}   raw bytes (application/octet-stream)
    POST /admin/videos/{id}/upload/complete     -> status "draft"

then PATCH /admin/videos/{id}/status {"status": "published"} to show it in the app.
"""

from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, Path, Query, Request, UploadFile, status

from app.core.config import settings
from app.core.deps import public_base_url, require_admin
from app.core.exceptions import FileTooLarge, UploadInvalidChunk
from app.schemas.common import ERROR_RESPONSES, ApiResponse, EmptyData, ok
from app.schemas.video import (
    AdminVideoOut,
    AdminVideoPage,
    DocumentPages,
    CategoryList,
    UploadProgress,
    VideoCreateRequest,
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


@router.post("", response_model=ApiResponse[AdminVideoOut], status_code=status.HTTP_201_CREATED)
async def create_video(payload: VideoCreateRequest, request: Request, admin: dict = Depends(require_admin)):
    """
    Step 1: create the video (status `uploading`) and get the chunk plan
    (`upload.chunk_size`, `upload.total_chunks`). Only MP4 (`video/mp4`).

    - `413 FILE_TOO_LARGE` (over MAX_VIDEO_SIZE_MB) · `415 UNSUPPORTED_MEDIA_TYPE`
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
    )
    return ok(video, "Upload started. Send the chunks, then call complete.")


@router.put("/{video_id}/upload/chunks/{index}", response_model=ApiResponse[UploadProgress])
async def upload_chunk(request: Request, video_id: str, index: int = Path(ge=0)):
    """
    Step 2: send chunk `index` (0-based) as the raw request body
    (`Content-Type: application/octet-stream`). Every chunk is exactly
    `chunk_size` bytes except the last. Chunks may arrive in any order and
    may be re-sent (e.g. after a network error).

    Chunk 0 must start like an MP4 file, otherwise `422 INVALID_MEDIA_FILE`.
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

    progress = await video_service.write_chunk(doc, index, bytes(body))
    return ok(progress, f"Chunk {index} received ({progress.received_chunks}/{progress.total_chunks})")


@router.get("/{video_id}/upload", response_model=ApiResponse[UploadProgress])
async def upload_status(video_id: str):
    """Which chunks have arrived and which are missing — use it to resume an interrupted upload."""
    return ok(await video_service.upload_progress(video_id), "Upload progress")


@router.post("/{video_id}/upload/complete", response_model=ApiResponse[AdminVideoOut])
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


@router.get("/categories", response_model=ApiResponse[CategoryList])
async def list_categories():
    """Every category used by any video (for the dashboard's category picker)."""
    return ok(CategoryList(categories=await video_service.categories(published_only=False)), "Categories fetched")


@router.get("/{video_id}", response_model=ApiResponse[AdminVideoOut])
async def get_video(video_id: str, request: Request):
    """One video, with a playback link (once uploaded) for previewing."""
    return ok(await video_service.get_admin(video_id, public_base_url(request)), "Video fetched successfully")


@router.patch("/{video_id}", response_model=ApiResponse[AdminVideoOut])
async def update_video(video_id: str, payload: VideoUpdateRequest, request: Request):
    """Edit title, description, category or display order. Only the fields you send change."""
    changes = payload.model_dump(include=payload.model_fields_set)
    if "title" in changes and changes["title"] is None:
        changes.pop("title")  # title can be changed but not removed
    if "sort_order" in changes and changes["sort_order"] is None:
        changes.pop("sort_order")
    return ok(await video_service.update_details(video_id, changes, public_base_url(request)), "Video updated")


@router.patch("/{video_id}/status", response_model=ApiResponse[AdminVideoOut])
async def set_video_status(video_id: str, payload: VideoStatusRequest, request: Request):
    """`published` shows the video in the app; `draft` hides it. `409 VIDEO_NOT_READY` while uploading."""
    video = await video_service.set_status(video_id, payload.status, public_base_url(request))
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


@router.put("/{video_id}/document", response_model=ApiResponse[AdminVideoOut])
async def upload_document(
    video_id: str,
    request: Request,
    background: BackgroundTasks,
    x_file_name: str = Header(..., alias="X-File-Name", description="Original file name, URL-encoded"),
):
    """
    Attach the video's document (replaces any existing one): **PDF, Word (.docx)
    or PowerPoint (.pptx)**, any size, sent as the raw request body.

    Documents are **view-only**: the original is never served. Word/PowerPoint
    are converted to pages in the background — `admin_document.status` goes
    `processing` → `ready` (or `failed` with `error`).
    """
    declared = request.headers.get("content-length")
    await document_service.upload(
        video_id, x_file_name, request.stream(), int(declared) if declared and declared.isdigit() else None
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
