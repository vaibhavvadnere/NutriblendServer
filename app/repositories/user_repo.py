"""
repositories/user_repo.py — Data access for the `users` collection.
"""

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


async def set_verified_at_if_missing(mobile_number: str, when: datetime) -> None:
    """Record the first verification time only — later logins leave it alone."""
    await users_collection.update_one(
        {"mobile_number": mobile_number, "verified_at": None},
        {"$set": {"verified_at": when}},
    )
