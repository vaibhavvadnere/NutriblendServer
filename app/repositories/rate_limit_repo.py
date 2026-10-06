"""
repositories/rate_limit_repo.py — Data access for the `rate_limits` collection.

One document per (scope, identifier, window). Each carries a TTL (`expires_at`)
so the collection cleans itself up.
"""

from datetime import datetime

from pymongo import ReturnDocument

from app.core.database import rate_limits_collection


async def increment(key: str, scope: str, identifier: str, expires_at: datetime) -> int:
    """Atomically add one to a window counter (creating it if needed) and
    return the new count."""
    doc = await rate_limits_collection.find_one_and_update(
        {"_id": key},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {"scope": scope, "identifier": identifier, "expires_at": expires_at},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["count"]


async def delete_for(scope: str, identifier: str) -> None:
    await rate_limits_collection.delete_many({"scope": scope, "identifier": identifier})
