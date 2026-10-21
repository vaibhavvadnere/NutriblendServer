"""
repositories/watch_repo.py — Data access for the `watch_history` collection.

One document per (user, video) the user has played — NOT one per play:

    {
      user_id:          ObjectId → users._id
      video_id:         ObjectId → videos._id
      play_count:       how many times they started it
      first_watched_at: the first play (UTC)
      last_watched_at:  the most recent play (UTC) — the history is newest first
    }

Deliberately a state row, not an event log. It grows with
"users × videos they touched", never with time, so it stays small enough for
the current Atlas tier. Per-minute detail (drop-off curves, seek patterns)
needs an event log and is a later decision.

Indexes (core/database.init_indexes): unique (user_id, video_id) — the real
guard when two taps arrive together —, (user_id, last_watched_at) for the
history list, and video_id for the ranking and for clean-up on video delete.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from pymongo import ReturnDocument

from app.core.database import watch_history_collection


async def record_play(user_id: ObjectId, video_id: ObjectId, dedupe_seconds: int) -> dict:
    """
    Record that the user pressed play. Returns the row afterwards.

    `play_count` is only incremented when the previous play was more than
    `dedupe_seconds` ago. Without that, one viewing inflates the count: the app
    re-posts after a seek, a re-buffer, a rotation or a quick back-and-forward,
    and every one of those would look like another play.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=dedupe_seconds)

    # Try the counted path first: only matches when the last play is old enough
    # (or this is the first play, handled by the upsert below).
    row = await watch_history_collection.find_one_and_update(
        {"user_id": user_id, "video_id": video_id, "last_watched_at": {"$lte": cutoff}},
        {"$inc": {"play_count": 1}, "$set": {"last_watched_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if row is not None:
        return row

    # Either the row is new, or the last play was recent. upsert covers both:
    # a new row starts at play_count 1; an existing recent one just moves its
    # last_watched_at forward without counting again.
    return await watch_history_collection.find_one_and_update(
        {"user_id": user_id, "video_id": video_id},
        {
            "$set": {"last_watched_at": now},
            "$setOnInsert": {
                "user_id": user_id,
                "video_id": video_id,
                "play_count": 1,
                "first_watched_at": now,
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def count_for_user(user_id: ObjectId) -> int:
    """How many DIFFERENT videos this user has played."""
    return await watch_history_collection.count_documents({"user_id": user_id})


async def plays_for_user(user_id: ObjectId) -> int:
    """Total plays across all videos for this user."""
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": None, "plays": {"$sum": "$play_count"}}},
    ]
    async for row in watch_history_collection.aggregate(pipeline):
        return int(row.get("plays") or 0)
    return 0


async def history_for_user(user_id: ObjectId, skip: int, limit: int) -> tuple[list[dict], int]:
    """That user's watched videos, most recently watched first, plus the total."""
    filters: dict[str, Any] = {"user_id": user_id}
    total = await watch_history_collection.count_documents(filters)
    cursor = (
        watch_history_collection.find(filters)
        .sort([("last_watched_at", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit)
    )
    return [row async for row in cursor], total


async def stats_for_videos(video_ids: list[ObjectId]) -> dict[ObjectId, dict]:
    """{video_id: {viewers, plays, last_watched_at}} for the given videos."""
    if not video_ids:
        return {}
    pipeline = [
        {"$match": {"video_id": {"$in": video_ids}}},
        {"$group": {
            "_id": "$video_id",
            "viewers": {"$sum": 1},
            "plays": {"$sum": "$play_count"},
            "last_watched_at": {"$max": "$last_watched_at"},
        }},
    ]
    return {
        row["_id"]: {
            "viewers": int(row.get("viewers") or 0),
            "plays": int(row.get("plays") or 0),
            "last_watched_at": row.get("last_watched_at"),
        }
        async for row in watch_history_collection.aggregate(pipeline)
    }


async def most_watched(skip: int, limit: int) -> tuple[list[dict], int]:
    """
    Videos ranked by how many DIFFERENT people played them, then by total plays.

    Ranking by viewers rather than plays on purpose: one enthusiast replaying a
    video twenty times should not outrank twenty people watching it once.
    """
    group = {
        "$group": {
            "_id": "$video_id",
            "viewers": {"$sum": 1},
            "plays": {"$sum": "$play_count"},
            "last_watched_at": {"$max": "$last_watched_at"},
        }
    }
    total = 0
    async for row in watch_history_collection.aggregate([group, {"$count": "n"}]):
        total = int(row.get("n") or 0)

    pipeline = [
        group,
        {"$sort": {"viewers": -1, "plays": -1, "_id": 1}},
        {"$skip": skip},
        {"$limit": limit},
    ]
    return [row async for row in watch_history_collection.aggregate(pipeline)], total


async def delete_for_video(video_id: ObjectId) -> int:
    result = await watch_history_collection.delete_many({"video_id": video_id})
    return result.deleted_count


async def delete_for_user(user_id: ObjectId) -> int:
    """An account was deleted: watch history is personal data and goes with it."""
    result = await watch_history_collection.delete_many({"user_id": user_id})
    return result.deleted_count
