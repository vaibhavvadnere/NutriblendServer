"""
services/watch_service.py — "Which videos has this user played?"

Scope on purpose: a play is recorded when the user presses play, and all that
is kept is one row per (user, video) with a play counter and timestamps. No
positions, no heartbeats, no event log — those need storage the current Atlas
tier does not have, and are a later decision.

What that still answers:
  * app       — "you have watched 14 videos"
  * dashboard — most watched videos, and what any one user has watched
"""

import math

from bson import ObjectId

from app.core.config import settings
from app.core.exceptions import VideoNotFound
from app.repositories import video_repo, watch_repo
from app.schemas.video import (
    MostWatchedPage,
    MostWatchedVideo,
    WatchStats,
    WatchStatus,
    WatchedVideo,
    WatchedVideoPage,
)
from app.services import rate_limiter

MAX_PAGE_SIZE = 100


def _object_id(video_id: str) -> ObjectId:
    if not ObjectId.is_valid(video_id):
        raise VideoNotFound()
    return ObjectId(video_id)


async def _throttle(user_id: ObjectId) -> None:
    await rate_limiter.enforce(rate_limiter.Rule(
        scope="watch_user_hour",
        identifier=str(user_id),
        limit=settings.WATCH_MAX_PLAYS_PER_HOUR,
        window_seconds=3600,
        message="Too many plays recorded. Please try again later.",
    ))


async def record_play(user_id: ObjectId, video_id: str) -> WatchStatus:
    """
    The app calls this when playback starts.

    Only a video the app may actually show can be recorded, which is the same
    rule as adding a favourite — otherwise a crafted request could write
    history for a draft.
    """
    await _throttle(user_id)
    oid = _object_id(video_id)
    if not await video_repo.find_visible_by_ids([oid]):
        raise VideoNotFound()

    row = await watch_repo.record_play(user_id, oid, settings.WATCH_DEDUPE_SECONDS)
    return WatchStatus(
        video_id=video_id,
        play_count=int(row.get("play_count") or 1),
        watched_videos_count=await watch_repo.count_for_user(user_id),
    )


async def stats_for_user(user_id: ObjectId) -> WatchStats:
    """The app's profile numbers. Counts videos, not plays."""
    return WatchStats(
        videos_count=await watch_repo.count_for_user(user_id),
        total_plays=await watch_repo.plays_for_user(user_id),
    )


async def history_for_user(user_id: ObjectId, page: int, page_size: int) -> WatchedVideoPage:
    """Admin view: what this user has watched, most recent first.

    A video deleted since being watched still appears, with null title and
    status — hiding it would quietly change the user's history."""
    page, page_size = max(page, 1), min(max(page_size, 1), MAX_PAGE_SIZE)
    rows, total = await watch_repo.history_for_user(user_id, (page - 1) * page_size, page_size)

    videos = {v["_id"]: v for v in await video_repo.find_by_ids([r["video_id"] for r in rows])}
    items = [
        WatchedVideo(
            video_id=str(r["video_id"]),
            title=(videos.get(r["video_id"]) or {}).get("title"),
            category=(videos.get(r["video_id"]) or {}).get("category"),
            status=(videos.get(r["video_id"]) or {}).get("status"),
            play_count=int(r.get("play_count") or 1),
            first_watched_at=r["first_watched_at"],
            last_watched_at=r["last_watched_at"],
        )
        for r in rows
    ]
    return WatchedVideoPage(items=items, total=total, page=page, page_size=page_size,
                            pages=max(math.ceil(total / page_size), 1))


async def most_watched(page: int, page_size: int) -> MostWatchedPage:
    """Admin view: the ranking. Only videos someone has actually played appear."""
    page, page_size = max(page, 1), min(max(page_size, 1), MAX_PAGE_SIZE)
    rows, total = await watch_repo.most_watched((page - 1) * page_size, page_size)

    videos = {v["_id"]: v for v in await video_repo.find_by_ids([r["_id"] for r in rows])}
    items = [
        MostWatchedVideo(
            video_id=str(r["_id"]),
            title=(videos.get(r["_id"]) or {}).get("title"),
            category=(videos.get(r["_id"]) or {}).get("category"),
            status=(videos.get(r["_id"]) or {}).get("status"),
            viewers=int(r.get("viewers") or 0),
            plays=int(r.get("plays") or 0),
            last_watched_at=r.get("last_watched_at"),
        )
        for r in rows
    ]
    return MostWatchedPage(items=items, total=total, page=page, page_size=page_size,
                           pages=max(math.ceil(total / page_size), 1))


async def forget_video(video_id: ObjectId) -> None:
    await watch_repo.delete_for_video(video_id)


async def forget_user(user_id: ObjectId) -> None:
    await watch_repo.delete_for_user(user_id)
