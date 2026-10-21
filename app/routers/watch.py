"""
routers/watch.py — Watch history.

    POST /videos/{video_id}/watch        record a play          (app session)
    (the user's watched count lives on GET /users/me/stats)
    GET  /admin/stats/most-watched       the ranking            (admin session)
    GET  /admin/users/{user_id}/watched  one user's history     (admin session)

Rules live in services/watch_service.py.
"""

from fastapi import APIRouter, Depends, Query

from app.core.config import settings
from app.core.deps import get_current_user, require_admin
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.video import MostWatchedPage, WatchStatus, WatchedVideoPage
from app.services import watch_service

video_watch_router = APIRouter(prefix=f"{settings.API_PREFIX}/videos", tags=["watch history"], responses=ERROR_RESPONSES)
admin_watch_router = APIRouter(prefix=f"{settings.API_PREFIX}/admin", tags=["watch history"], responses=ERROR_RESPONSES)


@video_watch_router.post("/{video_id}/watch", response_model=ApiResponse[WatchStatus])
async def record_watch(video_id: str, user: dict = Depends(get_current_user)):
    """
    Record that the user started playing this video. Call it once when
    playback begins — there are no heartbeats and no position tracking.

    Calls closer together than `WATCH_DEDUPE_SECONDS` (5 minutes) update the
    timestamp but do **not** count as another play, so a seek, a re-buffer or
    a screen rotation does not inflate the number.

    - `404 VIDEO_NOT_FOUND` — unknown, or not visible in the app.
    - `429 RATE_LIMITED` — too many plays recorded this hour.
    """
    return ok(await watch_service.record_play(user["_id"], video_id), "Play recorded")


@admin_watch_router.get("/stats/most-watched", response_model=ApiResponse[MostWatchedPage])
async def most_watched(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=watch_service.MAX_PAGE_SIZE),
    admin: dict = Depends(require_admin),
):
    """
    Videos ranked by how many **different** people played them, then by total plays.

    Lives under /admin/stats/ rather than /admin/videos/ on purpose: the latter
    already has GET /admin/videos/{video_id}, which would match "most-watched"
    as a video id and answer 404.

    Ranked by viewers rather than plays deliberately: one person replaying a
    video twenty times should not outrank twenty people watching it once. Both
    numbers are returned so you can see when they disagree.

    Videos nobody has played do not appear.
    """
    return ok(await watch_service.most_watched(page, page_size), "Most watched videos fetched")


@admin_watch_router.get("/users/{user_id}/watched", response_model=ApiResponse[WatchedVideoPage])
async def user_watch_history(
    user_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=watch_service.MAX_PAGE_SIZE),
    admin: dict = Depends(require_admin),
):
    """
    What one user has watched, most recently played first.

    A video deleted since it was watched still appears with a null title and
    status — dropping it would quietly rewrite the user's history.
    """
    from app.services import user_service

    user = await user_service.get_by_id(user_id)
    return ok(await watch_service.history_for_user(user["_id"], page, page_size),
              "Watch history fetched")
