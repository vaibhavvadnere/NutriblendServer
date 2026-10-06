"""
repositories/user_repo.py — Data access for the `users` collection.
"""

import re
from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import users_collection


class DuplicateUser(Exception):
    """An insert hit a unique index (mobile_number, or email when enforced)."""


def is_valid_id(user_id: str) -> bool:
    return ObjectId.is_valid(user_id)


async def find_by_mobile(mobile_number: str) -> Optional[dict]:
    return await users_collection.find_one({"mobile_number": mobile_number})


async def find_by_id(user_id: str) -> Optional[dict]:
    if not is_valid_id(user_id):
        return None
    return await users_collection.find_one({"_id": ObjectId(user_id)})


async def email_taken(email: str, except_mobile: Optional[str] = None) -> bool:
    """True if another account (other than `except_mobile`) uses this email."""
    query: dict[str, Any] = {"email": email}
    if except_mobile:
        query["mobile_number"] = {"$ne": except_mobile}
    return await users_collection.find_one(query) is not None


async def insert(doc: dict) -> dict:
    """Insert a new user. Raises DuplicateUser on a unique-index clash."""
    try:
        result = await users_collection.insert_one(doc)
    except DuplicateKeyError as exc:
        raise DuplicateUser(str(exc)) from exc
    doc["_id"] = result.inserted_id
    return doc


async def update_by_id(user_id: ObjectId, set_fields: dict) -> Optional[dict]:
    await users_collection.update_one({"_id": user_id}, {"$set": set_fields})
    return await users_collection.find_one({"_id": user_id})


async def update_by_mobile(
    mobile_number: str, set_fields: dict, unset_fields: Optional[list[str]] = None
) -> Optional[dict]:
    """Apply $set (and optional $unset) and return the updated document."""
    update: dict[str, Any] = {"$set": set_fields}
    if unset_fields:
        update["$unset"] = {field: "" for field in unset_fields}
    return await users_collection.find_one_and_update(
        {"mobile_number": mobile_number}, update, return_document=ReturnDocument.AFTER
    )


async def soft_delete(mobile_number: str, when: datetime) -> Optional[dict]:
    """
    Mark an account deleted. The mobile number stays on the row.

    Keeping the number is what blocks re-registration: `mobile_number` carries
    a unique index, so as long as the tombstone holds it, no new signup for that
    number can be created. Releasing it is an admin action, not something the
    user can do by deleting and starting over.

    Name and email are scrubbed, so the personal data goes while the account's
    shape — when it was created, when it was deleted — remains for audit.
    """
    doc = await users_collection.find_one({"mobile_number": mobile_number})
    if doc is None:
        return None
    return await users_collection.find_one_and_update(
        {"_id": doc["_id"]},
        {
            "$set": {
                "status": "deleted",
                "is_verified": False,
                "name": "Deleted user",
                "email": None,
                "deleted_at": when,
                "updated_at": when,
            },
            # An account that never expires on the pending TTL, and no longer
            # needs a login timestamp.
            "$unset": {"pending_expires_at": ""},
        },
        return_document=ReturnDocument.AFTER,
    )


async def set_verified_at_if_missing(mobile_number: str, when: datetime) -> None:
    """Record the first verification time only — later logins leave it alone."""
    await users_collection.update_one(
        {"mobile_number": mobile_number, "verified_at": None},
        {"$set": {"verified_at": when}},
    )


# ── Admin dashboard queries ──────────────────────────────────────────────────

# Documents written before `status` existed only have `is_verified`; derive a
# status for them the same way user_service.to_user_out does.
_EFFECTIVE_STATUS = {
    "$ifNull": [
        "$status",
        {"$cond": [{"$eq": ["$is_verified", True]}, "active", "pending"]},
    ]
}


# Dashboard statistics and the user list cover app users only:
# role != "admin". $ne also matches documents with no `role` field (old users).
_NOT_ADMIN: dict[str, Any] = {"role": {"$ne": "admin"}}

# Deleted accounts are tombstones — no name, no email, no usable number. They
# stay in the collection for audit but must not appear in the dashboard's user
# list or inflate its counts.
_NOT_DELETED: dict[str, Any] = {"status": {"$ne": "deleted"}}
_LIVE_USERS: dict[str, Any] = {**_NOT_ADMIN, **_NOT_DELETED}


async def count_all() -> int:
    return await users_collection.count_documents(_LIVE_USERS)


async def count_by_status() -> dict[str, int]:
    """{"active": n, "pending": n, "blocked": n} — statuses with no users are omitted."""
    pipeline = [{"$match": _LIVE_USERS}, {"$group": {"_id": _EFFECTIVE_STATUS, "count": {"$sum": 1}}}]
    return {row["_id"]: row["count"] async for row in users_collection.aggregate(pipeline)}


async def count_created_since(since: datetime) -> int:
    return await users_collection.count_documents({**_LIVE_USERS, "created_at": {"$gte": since}})


async def count_logged_in_since(since: datetime) -> int:
    return await users_collection.count_documents({**_LIVE_USERS, "last_login_at": {"$gte": since}})


async def signups_per_day(since: datetime, tz: str) -> dict[str, int]:
    """{"2026-09-26": n, ...} — accounts created per calendar day in `tz`."""
    pipeline = [
        {"$match": {**_LIVE_USERS, "created_at": {"$gte": since}}},
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at", "timezone": tz}},
                "count": {"$sum": 1},
            }
        },
    ]
    return {row["_id"]: row["count"] async for row in users_collection.aggregate(pipeline)}


async def search(
    query: Optional[str], status: Optional[str], skip: int, limit: int
) -> tuple[list[dict], int]:
    """
    Newest-first page of users, plus the total number of matches.

    `query` matches part of the mobile number when it is all digits, otherwise
    part of the name or email (case-insensitive).
    """
    filters: dict[str, Any] = dict(_LIVE_USERS)
    if status:
        filters["status"] = status
    if query:
        text = query.strip()
        digits = re.sub(r"\D", "", text)
        if digits and digits == text.replace(" ", "").lstrip("+"):
            filters["mobile_number"] = {"$regex": re.escape(digits[-10:])}
        else:
            pattern = {"$regex": re.escape(text), "$options": "i"}
            filters["$or"] = [{"name": pattern}, {"email": pattern}]

    total = await users_collection.count_documents(filters)
    cursor = (
        users_collection.find(filters)
        .sort([("created_at", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit)
    )
    return [doc async for doc in cursor], total
