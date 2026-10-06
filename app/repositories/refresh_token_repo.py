"""
repositories/refresh_token_repo.py — Data access for the `refresh_tokens` collection.

Stored document shape:
    {
      token_hash:    sha256 of the opaque token   (unique index)
      mobile_number: who it belongs to
      family_id:     ties rotations of one login session together
      created_at, expires_at (TTL index),
      revoked_at:    None while usable
      revoked_reason: "rotated" | "logout" | "logout_all" | "reuse_detected"
      replaced_by:   token_hash of the token that superseded this one
    }
"""

from datetime import datetime
from typing import Optional

from app.core.database import refresh_tokens_collection


async def insert(doc: dict) -> None:
    await refresh_tokens_collection.insert_one(doc)


async def find_by_hash(token_hash: str) -> Optional[dict]:
    return await refresh_tokens_collection.find_one({"token_hash": token_hash})


async def mark_rotated(token_id, replaced_by: str, when: datetime) -> None:
    await refresh_tokens_collection.update_one(
        {"_id": token_id},
        {"$set": {"revoked_at": when, "revoked_reason": "rotated", "replaced_by": replaced_by}},
    )


async def revoke_family(family_id: str, reason: str, when: datetime) -> int:
    """Revoke every still-active token in one login session. Returns the count."""
    result = await refresh_tokens_collection.update_many(
        {"family_id": family_id, "revoked_at": None},
        {"$set": {"revoked_at": when, "revoked_reason": reason}},
    )
    return result.modified_count


async def revoke_all_for_mobile(mobile_number: str, reason: str, when: datetime) -> int:
    """Revoke every still-active token for a user. Returns the count."""
    result = await refresh_tokens_collection.update_many(
        {"mobile_number": mobile_number, "revoked_at": None},
        {"$set": {"revoked_at": when, "revoked_reason": reason}},
    )
    return result.modified_count


async def count_active(mobile_number: str, now: datetime) -> int:
    return await refresh_tokens_collection.count_documents(
        {"mobile_number": mobile_number, "revoked_at": None, "expires_at": {"$gt": now}}
    )
