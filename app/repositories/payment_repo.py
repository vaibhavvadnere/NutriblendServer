"""
repositories/payment_repo.py — Data access for the `payment_orders` collection.

One document per checkout attempt (= one Razorpay order):
    {
      user_id:             ObjectId of the buyer
      mobile_number:       buyer's number at purchase time (for the dashboard)
      plan_id, plan_name, duration_days   (copied: later plan edits don't change old orders)
      amount, currency:    what Razorpay was asked to charge (paise)
      razorpay_order_id:   unique
      razorpay_payment_id: unique once paid (sparse)
      status:              "created" | "paid" | "failed" | "refunded"
      paid_via:            "verify" | "webhook"  — who confirmed it first
      failure_reason:      last failure text from Razorpay, if any
      created_at, paid_at, updated_at, refunded_at
    }

The status change created -> paid is a single conditional update, so when the
app's /verify and Razorpay's webhook arrive together exactly one of them wins
and the subscription is extended exactly once.
"""

from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from pymongo import ReturnDocument

from app.core.database import payment_orders_collection

STATUS_CREATED = "created"
STATUS_PAID = "paid"
STATUS_FAILED = "failed"
STATUS_REFUNDED = "refunded"


async def insert(doc: dict) -> dict:
    result = await payment_orders_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def find_by_razorpay_order_id(order_id: str) -> Optional[dict]:
    return await payment_orders_collection.find_one({"razorpay_order_id": order_id})


async def find_by_razorpay_payment_id(payment_id: str) -> Optional[dict]:
    return await payment_orders_collection.find_one({"razorpay_payment_id": payment_id})


async def mark_paid_if_unpaid(order_id: str, payment_id: str, via: str, when: datetime) -> Optional[dict]:
    """created/failed -> paid. Returns the updated order, or None if it was already
    paid/refunded (someone else got there first) or does not exist."""
    return await payment_orders_collection.find_one_and_update(
        {"razorpay_order_id": order_id, "status": {"$in": [STATUS_CREATED, STATUS_FAILED]}},
        {"$set": {
            "status": STATUS_PAID,
            "razorpay_payment_id": payment_id,
            "paid_via": via,
            "paid_at": when,
            "updated_at": when,
        }},
        return_document=ReturnDocument.AFTER,
    )


async def mark_failed_if_created(order_id: str, reason: Optional[str], when: datetime) -> None:
    """A failed attempt. Only touches orders not yet paid — Checkout lets the user
    retry on the same order, so a later success still wins."""
    await payment_orders_collection.update_one(
        {"razorpay_order_id": order_id, "status": STATUS_CREATED},
        {"$set": {"status": STATUS_FAILED, "failure_reason": reason, "updated_at": when}},
    )


async def mark_refunded(payment_id: str, when: datetime) -> Optional[dict]:
    """paid -> refunded. Returns the order only if this call changed it."""
    return await payment_orders_collection.find_one_and_update(
        {"razorpay_payment_id": payment_id, "status": STATUS_PAID},
        {"$set": {"status": STATUS_REFUNDED, "refunded_at": when, "updated_at": when}},
        return_document=ReturnDocument.AFTER,
    )


async def list_orders(
    status: Optional[str], user_id: Optional[ObjectId], page: int, page_size: int
) -> tuple[list[dict], int]:
    """Newest first, for the dashboard."""
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    if user_id is not None:
        query["user_id"] = user_id
    total = await payment_orders_collection.count_documents(query)
    cursor = (
        payment_orders_collection.find(query)
        .sort("created_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return await cursor.to_list(length=page_size), total


async def paid_orders_for_user(user_id: ObjectId) -> list[dict]:
    """Every still-paid order of a user, oldest first (used to rebuild after a refund)."""
    cursor = payment_orders_collection.find({"user_id": user_id, "status": STATUS_PAID}).sort("paid_at", 1)
    return await cursor.to_list(length=None)
