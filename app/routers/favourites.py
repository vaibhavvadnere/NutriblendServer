"""
routers/favourites.py — The ♡ in the app (app session required).

    PUT    /videos/{video_id}/favourite   add    (idempotent)
    DELETE /videos/{video_id}/favourite   remove (idempotent)
    GET    /users/me/favourites           my favourites, newest first

Adding / removing works even while videos are locked behind a subscription.
The list hands out playback links, so it needs a subscription exactly like
GET /videos (only while PAYMENTS_LOCK_VIDEOS is on).

Rules live in services/favourite_service.py.
"""

from fastapi import APIRouter, Depends, Query, Request

from app.core.config import settings
from app.core.deps import get_current_user, public_base_url, require_subscription
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.video import FavouriteStatus, VideoPage
from app.services import favourite_service, video_service

video_favourite_router = APIRouter(prefix=f"{settings.API_PREFIX}/videos", tags=["favourites"], responses=ERROR_RESPONSES)
my_favourites_router = APIRouter(prefix=f"{settings.API_PREFIX}/users", tags=["favourites"], responses=ERROR_RESPONSES)


@video_favourite_router.put("/{video_id}/favourite", response_model=ApiResponse[FavouriteStatus])
async def add_favourite(video_id: str, user: dict = Depends(get_current_user)):
    """
    Mark a video as favourite. Calling it again changes nothing.

    - `404 VIDEO_NOT_FOUND` — unknown, or not visible in the app.
    - `409 FAVOURITES_LIMIT_REACHED` — the user already has the maximum.
    """
    return ok(await favourite_service.add(user["_id"], video_id), "Added to favourites")


@video_favourite_router.delete("/{video_id}/favourite", response_model=ApiResponse[FavouriteStatus])
async def remove_favourite(video_id: str, user: dict = Depends(get_current_user)):
    """Remove a video from favourites. Calling it again (or for a video that isn't one) changes nothing."""
    return ok(await favourite_service.remove(user["_id"], video_id), "Removed from favourites")


@my_favourites_router.get("/me/favourites", response_model=ApiResponse[VideoPage])
async def list_favourites(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=video_service.MAX_PAGE_SIZE),
    user: dict = Depends(require_subscription),
):
    """
    The user's favourites that are visible in the app, newest favourite first.
    A video that is unpublished later drops out of this list (and returns if republished).
    """
    return ok(await favourite_service.list_for_user(user["_id"], page, page_size, public_base_url(request)),
              "Favourites fetched")
