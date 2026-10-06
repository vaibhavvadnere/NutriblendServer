"""
repositories/video_repo.py — Data access for the `videos` collection.

Document shape:
    {
      title, description, category, sort_order,
      status:     "uploading" | "draft" | "published",
      file:       { key, size, content_type, original_name, duration_seconds?, video_codec? },
      thumbnail:  { key, size, content_type } | absent,
      upload:     { chunk_size, total_chunks, received: [int], state: "receiving"|"assembling",
                    expires_at }                 # only while status == "uploading"
      created_by, created_at, updated_at, uploaded_at?, published_at?
    }
"""

import re
from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from pymongo import ReturnDocument

from app.core.database import videos_collection


def is_valid_id(video_id: str) -> bool:
    return ObjectId.is_valid(video_id)


async def insert(doc: dict) -> dict:
    result = await videos_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def find_by_id(video_id: str) -> Optional[dict]:
    if not is_valid_id(video_id):
        return None
    return await videos_collection.find_one({"_id": ObjectId(video_id)})


async def update(video_id: ObjectId, set_fields: dict, unset_fields: Optional[list[str]] = None) -> Optional[dict]:
    update_doc: dict[str, Any] = {"$set": set_fields}
    if unset_fields:
        update_doc["$unset"] = {f: "" for f in unset_fields}
    return await videos_collection.find_one_and_update(
        {"_id": video_id}, update_doc, return_document=ReturnDocument.AFTER
    )


async def mark_chunk_received(video_id: ObjectId, index: int) -> Optional[dict]:
    """Record one chunk, only while the upload is still receiving."""
    return await videos_collection.find_one_and_update(
        {"_id": video_id, "status": "uploading", "upload.state": "receiving"},
        {"$addToSet": {"upload.received": index}},
        return_document=ReturnDocument.AFTER,
    )


async def claim_for_assembly(video_id: ObjectId) -> Optional[dict]:
    """receiving -> assembling, atomically, so two 'complete' calls can't race."""
    return await videos_collection.find_one_and_update(
        {"_id": video_id, "status": "uploading", "upload.state": "receiving"},
        {"$set": {"upload.state": "assembling"}},
        return_document=ReturnDocument.AFTER,
    )


async def release_assembly(video_id: ObjectId) -> None:
    await videos_collection.update_one(
        {"_id": video_id, "upload.state": "assembling"}, {"$set": {"upload.state": "receiving"}}
    )


async def delete(video_id: ObjectId) -> None:
    await videos_collection.delete_one({"_id": video_id})


async def find_expired_uploads(now: datetime) -> list[dict]:
    cursor = videos_collection.find({"status": "uploading", "upload.expires_at": {"$lt": now}})
    return [doc async for doc in cursor]


def _text_filter(query: Optional[str]) -> dict:
    if not query or not query.strip():
        return {}
    pattern = {"$regex": re.escape(query.strip()), "$options": "i"}
    return {"$or": [{"title": pattern}, {"description": pattern}, {"category": pattern}]}


async def search_admin(
    query: Optional[str], status: Optional[str], category: Optional[str], skip: int, limit: int
) -> tuple[list[dict], int]:
    """Newest first. Everything, including drafts and uploads in progress."""
    filters: dict[str, Any] = _text_filter(query)
    if status:
        filters["status"] = status
    if category:
        filters["category"] = category
    total = await videos_collection.count_documents(filters)
    cursor = videos_collection.find(filters).sort([("created_at", -1), ("_id", -1)]).skip(skip).limit(limit)
    return [doc async for doc in cursor], total


async def search_published(category: Optional[str], skip: int, limit: int) -> tuple[list[dict], int]:
    """App order: sort_order (low first), then most recently published."""
    filters: dict[str, Any] = {"status": "published"}
    if category:
        filters["category"] = category
    total = await videos_collection.count_documents(filters)
    cursor = (
        videos_collection.find(filters)
        .sort([("sort_order", 1), ("published_at", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit)
    )
    return [doc async for doc in cursor], total


async def categories(published_only: bool) -> list[str]:
    filters: dict[str, Any] = {"category": {"$nin": [None, ""]}}
    if published_only:
        filters["status"] = "published"
    values = await videos_collection.distinct("category", filters)
    return sorted(values, key=str.lower)


async def count_by_status() -> dict[str, int]:
    pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    return {row["_id"]: row["count"] async for row in videos_collection.aggregate(pipeline)}
