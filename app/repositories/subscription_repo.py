"""
repositories/subscription_repo.py — Data access for the `subscriptions` collection.

One document per user (unique user_id); a purchase extends it, never adds a second:
    {
      user_id:         ObjectId                      (unique index)
      plan_id, plan_name: of the most recent purchase / grant
      started_at:      start of the current continuous access period
      expires_at:      access ends here (UTC)
      source:          "payment" | "admin"           — what last changed it
      last_order_id:   razorpay_order_id of the last purchase, if any
      ended_reason:    "refund" | "account_deleted" | "admin_revoke" when cut short
      history:         [{at, action, days, source, order_id, by}]  audit trail
      version:         incremented on every write (optimistic concurrency)
      created_at, updated_at
    }

Writes are compare-and-set on `version`: two purchases landing at the same
moment can't both read the old expiry and lose one of the extensions.
"""

from typing import Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.database import subscriptions_collection


class ConcurrentUpdate(Exception):
    """Another write got in first; re-read and try again."""


async def find_by_user(user_id: ObjectId) -> Optional[dict]:
    return await subscriptions_collection.find_one({"user_id": user_id})


async def insert_new(doc: dict) -> dict:
    """First subscription for a user. ConcurrentUpdate if one was created meanwhile."""
    try:
        result = await subscriptions_collection.insert_one({**doc, "version": 1})
    except DuplicateKeyError as exc:
        raise ConcurrentUpdate() from exc
    return {**doc, "_id": result.inserted_id, "version": 1}


async def update_if_version(user_id: ObjectId, version: int, set_fields: dict, history_entry: dict) -> dict:
    """Apply the change only if nobody wrote since we read `version`."""
    doc = await subscriptions_collection.find_one_and_update(
        {"user_id": user_id, "version": version},
        {"$set": set_fields, "$inc": {"version": 1}, "$push": {"history": history_entry}},
        return_document=True,
    )
    if doc is None:
        raise ConcurrentUpdate()
    return doc


async def count_active(now) -> int:
    return await subscriptions_collection.count_documents({"expires_at": {"$gt": now}})
