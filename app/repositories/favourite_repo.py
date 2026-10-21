"""
repositories/favourite_repo.py — Data access for the `favourites` collection.

One document per (user, video) the user marked as favourite:
    {
      user_id:    ObjectId → users._id
      video_id:   ObjectId → videos._id
      created_at: when it was added (UTC) — the list is newest first
    }

Indexes (core/database.init_indexes): unique (user_id, video_id) — the real
guard against duplicates when two taps arrive together —, (user_id, created_at)
for the list, and video_id for the clean-up when a video is deleted.
"""

from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.database import favourites_collection


async def add(user_id: ObjectId, video_id: ObjectId) -> bool:
    """Insert the pair. True if it was new, False if it was already there (idempotent)."""
    try:
        await favourites_collection.insert_one(
            {"user_id": user_id, "video_id": video_id, "created_at": datetime.now(timezone.utc)}
        )
        return True
    except DuplicateKeyError:
        return False


async def remove(user_id: ObjectId, video_id: ObjectId) -> bool:
    """Delete the pair. True if something was removed."""
    result = await favourites_collection.delete_one({"user_id": user_id, "video_id": video_id})
    return result.deleted_count == 1


async def exists(user_id: ObjectId, video_id: ObjectId) -> bool:
    return await favourites_collection.count_documents({"user_id": user_id, "video_id": video_id}, limit=1) > 0


async def count_for_user(user_id: ObjectId) -> int:
    return await favourites_collection.count_documents({"user_id": user_id})


async def video_ids_for_user(user_id: ObjectId) -> list[ObjectId]:
    """All of the user's favourite video ids, newest first (bounded by FAVOURITES_MAX_PER_USER)."""
    cursor = favourites_collection.find({"user_id": user_id}, {"video_id": 1}).sort([("created_at", -1), ("_id", -1)])
    return [doc["video_id"] async for doc in cursor]


async def which_are_favourites(user_id: ObjectId, video_ids: list[ObjectId]) -> set[ObjectId]:
    """The subset of `video_ids` this user has favourited (one query for a whole page of videos)."""
    if not video_ids:
        return set()
    cursor = favourites_collection.find({"user_id": user_id, "video_id": {"$in": video_ids}}, {"video_id": 1})
    return {doc["video_id"] async for doc in cursor}


async def delete_for_video(video_id: ObjectId) -> int:
    """A video was deleted: drop every user's favourite of it."""
    result = await favourites_collection.delete_many({"video_id": video_id})
    return result.deleted_count


async def delete_for_user(user_id: ObjectId) -> int:
    """An account was deleted: drop all of its favourites."""
    result = await favourites_collection.delete_many({"user_id": user_id})
    return result.deleted_count
