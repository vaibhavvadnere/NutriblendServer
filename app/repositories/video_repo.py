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
from datetime import datetime, timezone
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
    update_doc: dict[str, Any] = {"$set": set_fields} if set_fields else {}  # Mongo refuses an empty $set
    if unset_fields:
        update_doc["$unset"] = {f: "" for f in unset_fields}
    return await videos_collection.find_one_and_update(
        {"_id": video_id}, update_doc, return_document=ReturnDocument.AFTER
    )


async def mark_chunk_received(video_id: ObjectId, index: int, md5_hex: Optional[str] = None) -> Optional[dict]:
    """Record one chunk (and its MD5), only while the upload is still receiving."""
    update: dict[str, Any] = {"$addToSet": {"upload.received": index}}
    if md5_hex:
        update["$set"] = {f"upload.md5.{index}": md5_hex}
    return await videos_collection.find_one_and_update(
        {"_id": video_id, "status": "uploading", "upload.state": "receiving"},
        update,
        return_document=ReturnDocument.AFTER,
    )


async def expect_chunks(video_id: ObjectId, md5_hex: dict[int, str]) -> Optional[dict]:
    """Record the MD5s the client announced for chunks it sends straight to storage."""
    return await videos_collection.find_one_and_update(
        {"_id": video_id, "status": "uploading", "upload.state": "receiving"},
        {"$set": {f"upload.md5.{i}": h for i, h in md5_hex.items()}},
        return_document=ReturnDocument.AFTER,
    )


async def mark_chunks_received(video_id: ObjectId, indexes: list[int]) -> Optional[dict]:
    """Record chunks found in storage (sent there directly by the browser)."""
    return await videos_collection.find_one_and_update(
        {"_id": video_id, "status": "uploading", "upload.state": "receiving"},
        {"$addToSet": {"upload.received": {"$each": indexes}}},
        return_document=ReturnDocument.AFTER,
    )


async def forget_chunks(video_id: ObjectId, indexes: list[int]) -> None:
    """Mark chunks as not received (they must be sent again)."""
    await videos_collection.update_one(
        {"_id": video_id},
        {"$pull": {"upload.received": {"$in": indexes}},
         "$unset": {f"upload.md5.{i}": "" for i in indexes}},
    )


async def find_by_sha256(sha256: str) -> Optional[dict]:
    """The oldest video whose file has this SHA-256 (any status)."""
    # an optimized video keeps the fingerprint of the original file it came from
    cursor = videos_collection.find(
        {"$or": [{"file.sha256": sha256}, {"file.source_sha256": sha256}]}
    ).sort("created_at", 1).limit(1)
    docs = [d async for d in cursor]
    return docs[0] if docs else None


async def claim_optimization() -> Optional[dict]:
    """The oldest queued video, marked as running (atomic: two workers never take the same one)."""
    now = datetime.now(timezone.utc)
    return await videos_collection.find_one_and_update(
        {"optimization.state": "queued"},
        {"$set": {"optimization.state": "running", "optimization.started_at": now, "optimization.progress": 0.0}},
        sort=[("optimization.queued_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def requeue_running_optimizations() -> int:
    """After a restart: jobs that were running are queued again."""
    result = await videos_collection.update_many(
        {"optimization.state": "running"}, {"$set": {"optimization.state": "queued", "optimization.progress": 0.0}}
    )
    return result.modified_count


async def staged_upload_bytes() -> int:
    """Bytes still to arrive for uploads that are being received into the staging folder."""
    total = 0
    async for d in videos_collection.find({"status": "uploading", "file.staged": True}, {"file.size": 1, "upload": 1}):
        up = d.get("upload") or {}
        done = len(up.get("received", [])) * up.get("chunk_size", 0)
        total += max(d["file"]["size"] - done, 0)
    return total


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
    filters["replaces"] = {"$exists": False}       # unfinished replacement uploads are not videos of their own
    if status:
        filters["status"] = status
    if category:
        filters["category"] = category
    total = await videos_collection.count_documents(filters)
    cursor = videos_collection.find(filters).sort([("created_at", -1), ("_id", -1)]).skip(skip).limit(limit)
    return [doc async for doc in cursor], total


def visible_filter() -> dict[str, Any]:
    """Videos the app may show: published, and — if they have a document — only once it is ready."""
    return {
        "status": "published",
        "file.staged": {"$ne": True},
        "$or": [{"document": {"$exists": False}}, {"document": None}, {"document.status": "ready"}],
    }


async def publish_if_waiting(video_id: ObjectId) -> Optional[dict]:
    """The document just became ready: publish the draft that was waiting for it (atomic; at most once)."""
    now = datetime.now(timezone.utc)
    return await videos_collection.find_one_and_update(
        {
            "_id": video_id, "publish_when_ready": True, "status": "draft",
            # published only when both the document (if any) and the optimization (if any) are done
            "$and": [
                {"$or": [{"document": {"$exists": False}}, {"document": None}, {"document.status": "ready"}]},
                {"$or": [{"optimization": {"$exists": False}}, {"optimization.state": "done"}]},
            ],
        },
        {"$set": {"status": "published", "published_at": now, "updated_at": now}, "$unset": {"publish_when_ready": ""}},
        return_document=ReturnDocument.AFTER,
    )


async def search_published(category: Optional[str], skip: int, limit: int) -> tuple[list[dict], int]:
    """App order: sort_order (low first), then most recently published."""
    filters: dict[str, Any] = visible_filter()
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
        filters.update(visible_filter())
    values = await videos_collection.distinct("category", filters)
    return sorted(values, key=str.lower)


def _title_pattern(title: str):
    words = [re.escape(w) for w in title.split()]
    return re.compile(r"^\s*" + r"\s+".join(words) + r"\s*$", re.IGNORECASE)


async def find_by_title(title: str, exclude_id: Optional[ObjectId] = None) -> list[dict]:
    """Videos whose title is the same apart from capitals and extra spaces (unfinished replacements excluded)."""
    if not title.split():
        return []
    filters: dict[str, Any] = {"title": _title_pattern(title), "replaces": {"$exists": False}}
    if exclude_id:
        filters["_id"] = {"$ne": exclude_id}
    return [doc async for doc in videos_collection.find(filters).sort([("created_at", -1)]).limit(10)]


async def count_by_status() -> dict[str, int]:
    pipeline = [{"$match": {"replaces": {"$exists": False}}}, {"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    return {row["_id"]: row["count"] async for row in videos_collection.aggregate(pipeline)}


async def all_for_check() -> list[dict]:
    """Every video with only the fields the storage check needs."""
    projection = {"title": 1, "status": 1, "created_at": 1, "file.key": 1, "file.staged": 1, "thumbnail.key": 1, "document.key": 1}
    return [doc async for doc in videos_collection.find({}, projection)]
