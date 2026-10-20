"""
repositories/plan_repo.py — Data access for the `plans` collection.

Stored document shape:
    {
      name:          "Yearly"                 (shown in the app)
      description:   optional text
      amount:        price in paise (₹999 = 99900) — the ONLY price ever charged
      currency:      "INR"
      duration_days: how long one purchase lasts
      active:        false = hidden from the app, kept for old orders
      sort_order:    display order (ascending)
      created_at, updated_at
    }
"""

from typing import Optional

from bson import ObjectId
from pymongo import ReturnDocument

from app.core.database import plans_collection


def is_valid_id(plan_id: str) -> bool:
    return ObjectId.is_valid(plan_id)


async def insert(doc: dict) -> dict:
    result = await plans_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def find_by_id(plan_id: str) -> Optional[dict]:
    if not is_valid_id(plan_id):
        return None
    return await plans_collection.find_one({"_id": ObjectId(plan_id)})


async def list_plans(active_only: bool) -> list[dict]:
    query = {"active": True} if active_only else {}
    cursor = plans_collection.find(query).sort([("sort_order", 1), ("amount", 1)])
    return await cursor.to_list(length=None)


async def update(plan_id: ObjectId, set_fields: dict) -> Optional[dict]:
    return await plans_collection.find_one_and_update(
        {"_id": plan_id}, {"$set": set_fields}, return_document=ReturnDocument.AFTER
    )
