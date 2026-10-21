"""
services/favourite_service.py — Videos a user marks as favourite (the ♡ in the app).

Rules
-----
* Only videos the app can show (published, document ready) can be ADDED.
  Hidden or unknown ids answer 404 VIDEO_NOT_FOUND — nothing leaks about drafts.
* Adding twice / removing twice is harmless (idempotent): no duplicates, no error.
  The unique (user_id, video_id) index is the real guard when two taps race.
* Removing never checks visibility, so a heart can always be cleared.
* A video that is unpublished later disappears from the list, but the favourite
  is KEPT — republish it and it comes back. Deleting the video deletes its
  favourites; deleting the account deletes the user's favourites.
* At most FAVOURITES_MAX_PER_USER per user (409 FAVOURITES_LIMIT_REACHED).
  Counted before inserting, so two simultaneous adds at the very limit may let
  one extra through — harmless, the cap only exists to stop abuse.
* Favouriting is allowed even while videos are locked behind a subscription
  (the user can save what they want to buy); the LIST hands out playback links,
  so its endpoint is subscription-gated like GET /videos.
"""

import logging
import math

from bson import ObjectId

from app.core.config import settings
from app.core.exceptions import FavouritesLimitReached, VideoNotFound
from app.repositories import favourite_repo, video_repo
from app.schemas.video import FavouriteStatus, FavouritesStats, VideoOut, VideoPage
from app.services import rate_limiter, video_service

logger = logging.getLogger("nutriblend.favourites")


def _video_object_id(video_id: str) -> ObjectId:
    if not video_repo.is_valid_id(video_id):
        raise VideoNotFound()
    return ObjectId(video_id)


async def _throttle(user_id: ObjectId) -> None:
    await rate_limiter.enforce(rate_limiter.Rule(
        scope="favourite_user_hour",
        identifier=str(user_id),
        limit=settings.FAVOURITES_MAX_CHANGES_PER_HOUR,
        window_seconds=3600,
        message="Too many changes to favourites. Please try again later.",
    ))


async def _status(user_id: ObjectId, video_id: str, is_favourite: bool) -> FavouriteStatus:
    return FavouriteStatus(
        video_id=video_id,
        is_favourite=is_favourite,
        favourites_count=await favourite_repo.count_for_user(user_id),
        favourites_limit=settings.FAVOURITES_MAX_PER_USER,
    )


async def add(user_id: ObjectId, video_id: str) -> FavouriteStatus:
    await _throttle(user_id)
    await video_service.published_doc(video_id)                 # 404 unless the app can show it
    vid = ObjectId(video_id)
    if not await favourite_repo.exists(user_id, vid):
        if await favourite_repo.count_for_user(user_id) >= settings.FAVOURITES_MAX_PER_USER:
            raise FavouritesLimitReached(details={"limit": settings.FAVOURITES_MAX_PER_USER})
        await favourite_repo.add(user_id, vid)
    return await _status(user_id, video_id, True)


async def remove(user_id: ObjectId, video_id: str) -> FavouriteStatus:
    await _throttle(user_id)
    await favourite_repo.remove(user_id, _video_object_id(video_id))
    return await _status(user_id, video_id, False)


async def list_for_user(user_id: ObjectId, page: int, page_size: int, base_url: str) -> VideoPage:
    """The user's favourites the app can show, newest favourite first."""
    page, page_size = max(page, 1), min(max(page_size, 1), video_service.MAX_PAGE_SIZE)
    ordered_ids = await favourite_repo.video_ids_for_user(user_id)
    visible = {doc["_id"]: doc for doc in await video_repo.find_visible_by_ids(ordered_ids)}
    shown = [visible[i] for i in ordered_ids if i in visible]
    total = len(shown)
    start = (page - 1) * page_size
    items = []
    for doc in shown[start:start + page_size]:
        out = video_service.to_app_out(doc, base_url)
        out.is_favourite = True
        items.append(out)
    return VideoPage(items=items, total=total, page=page, page_size=page_size,
                     pages=max(math.ceil(total / page_size), 1))


async def stats_for_user(user_id: ObjectId) -> FavouritesStats:
    """
    The two favourite counts, without touching video documents or minting any
    playback links — cheap enough to call on every app launch.
    """
    total = await favourite_repo.count_for_user(user_id)
    visible = 0
    if total:
        visible = await video_repo.count_visible_by_ids(
            await favourite_repo.video_ids_for_user(user_id)
        )
    return FavouritesStats(
        count=total,
        visible_count=visible,
        limit=settings.FAVOURITES_MAX_PER_USER,
    )


async def mark(user_id: ObjectId, videos: list[VideoOut]) -> None:
    """Set `is_favourite` on videos about to be returned to this user (one query for the whole page)."""
    ids = [ObjectId(v.id) for v in videos]
    favourites = await favourite_repo.which_are_favourites(user_id, ids)
    for v in videos:
        v.is_favourite = ObjectId(v.id) in favourites


async def forget_video(video_id: ObjectId) -> None:
    removed = await favourite_repo.delete_for_video(video_id)
    if removed:
        logger.info("Removed %d favourite(s) of deleted video %s", removed, video_id)


async def forget_user(user_id: ObjectId) -> None:
    removed = await favourite_repo.delete_for_user(user_id)
    if removed:
        logger.info("Removed %d favourite(s) of deleted account %s", removed, user_id)
