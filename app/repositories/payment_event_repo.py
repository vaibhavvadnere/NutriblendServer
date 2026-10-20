"""
repositories/payment_event_repo.py — Data access for `payment_events`.

One document per Razorpay webhook delivery we processed:
    {
      event_id:          X-Razorpay-Event-Id (unique) — same id = same delivery
      event:             "payment.captured" | "order.paid" | "payment.failed" | "refund.processed" | ...
      razorpay_order_id, razorpay_payment_id
      result:            what we did: "granted" | "already_done" | "failed_recorded" |
                         "refunded" | "ignored_unknown_order" | "ignored_mismatch" | "ignored_event"
      received_at        (TTL: kept one year)
    }
"""

from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.core.database import payment_events_collection


async def seen(event_id: str) -> bool:
    return await payment_events_collection.find_one({"event_id": event_id}) is not None


async def record(doc: dict) -> None:
    """Store a processed delivery. A concurrent duplicate is harmless (ignored)."""
    try:
        await payment_events_collection.insert_one(doc)
    except DuplicateKeyError:
        pass


async def for_order(order_id: str) -> list[dict]:
    cursor = payment_events_collection.find({"razorpay_order_id": order_id}).sort("received_at", 1)
    return await cursor.to_list(length=None)
