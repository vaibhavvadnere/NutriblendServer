"""
routers/videos.py — Videos for the mobile app (app session required).

Only *published* videos are visible. Each comes with signed `playback_url` and
`thumbnail_url` links that expire (MEDIA_URL_TTL_SECONDS); re-fetch the video
for fresh links.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from app.core.config import settings
from app.core.deps import get_current_user, public_base_url
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.video import CategoryList, DocumentPages, VideoOut, VideoPage
from app.services import document_service, video_service

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/videos",
    tags=["videos"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(get_current_user)],
)


@router.get("", response_model=ApiResponse[VideoPage])
async def list_videos(
    request: Request,
    category: Optional[str] = Query(default=None, max_length=40),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=video_service.MAX_PAGE_SIZE),
):
    """Published videos in display order (`sort_order`, then newest)."""
    return ok(await video_service.list_published(category, page, page_size, public_base_url(request)), "Videos fetched successfully")


@router.get("/categories", response_model=ApiResponse[CategoryList])
async def list_categories():
    """Categories that have at least one published video."""
    return ok(CategoryList(categories=await video_service.categories(published_only=True)), "Categories fetched")


@router.get("/{video_id}", response_model=ApiResponse[VideoOut])
async def get_video(video_id: str, request: Request):
    """One published video (404 for drafts or unknown ids)."""
    return ok(await video_service.get_published(video_id, public_base_url(request)), "Video fetched successfully")


@router.get("/{video_id}/document", response_model=ApiResponse[DocumentPages])
async def get_document(video_id: str, request: Request):
    """
    The video's document as **page images** (view-only — the original file is
    never available). Show `pages` in order; links expire at `links_expire_at`.

    - `404 DOCUMENT_NOT_FOUND` — the video has no document.
    - `409 DOCUMENT_NOT_READY` — still being prepared.
    """
    video = await video_service.published_doc(video_id)
    return ok(document_service.pages_for(video, public_base_url(request)), "Document pages")
