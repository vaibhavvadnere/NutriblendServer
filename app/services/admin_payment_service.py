"""
services/admin_payment_service.py — Dashboard views of payments and subscriptions,
and manual access changes by an admin.

Works whether or not PAYMENTS_ENABLED is on, so plans can be prepared and
dealer/bundle access granted before in-app payments are switched on.
"""

import logging
import math
from typing import Optional

from bson import ObjectId

from app.core.exceptions import InvalidUserId
from app.repositories import payment_repo, subscription_repo
from app.schemas.payment import (
    PaymentOrderOut,
    PaymentOrderPage,
    SubscriptionHistoryItem,
    UserSubscriptionAdminOut,
)
from app.services import subscription_service, user_service
from app.utils.phone import mask

logger = logging.getLogger("nutriblend.payments.admin")

MAX_PAGE_SIZE = 100


def to_order_out(doc: dict) -> PaymentOrderOut:
    return PaymentOrderOut(
        id=str(doc["_id"]),
        user_id=str(doc["user_id"]),
        mobile_number=doc.get("mobile_number"),
        plan_id=doc["plan_id"],
        plan_name=doc["plan_name"],
        duration_days=doc["duration_days"],
        amount=doc["amount"],
        currency=doc["currency"],
        razorpay_order_id=doc["razorpay_order_id"],
        razorpay_payment_id=doc.get("razorpay_payment_id"),
        status=doc["status"],
        paid_via=doc.get("paid_via"),
        failure_reason=doc.get("failure_reason"),
        created_at=doc["created_at"],
        paid_at=doc.get("paid_at"),
        refunded_at=doc.get("refunded_at"),
    )


async def list_orders(status: Optional[str], user_id: Optional[str], page: int, page_size: int) -> PaymentOrderPage:
    uid = None
    if user_id:
        if not ObjectId.is_valid(user_id):
            raise InvalidUserId()
        uid = ObjectId(user_id)
    page_size = min(page_size, MAX_PAGE_SIZE)
    items, total = await payment_repo.list_orders(status, uid, page, page_size)
    return PaymentOrderPage(
        items=[to_order_out(d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, math.ceil(total / page_size)),
    )


async def user_subscription(user_id: str) -> UserSubscriptionAdminOut:
    user = await user_service.get_by_id(user_id)  # InvalidUserId / AccountNotFound
    doc = await subscription_repo.find_by_user(user["_id"])
    history = [
        SubscriptionHistoryItem(**{**h, "at": subscription_service._utc(h.get("at"))})
        for h in reversed((doc or {}).get("history") or [])
    ]
    return UserSubscriptionAdminOut(
        subscription=subscription_service.to_subscription_out(doc),
        source=(doc or {}).get("source"),
        ended_reason=(doc or {}).get("ended_reason"),
        history=history,
    )


async def grant(user_id: str, days: int, note: Optional[str], admin: dict) -> UserSubscriptionAdminOut:
    user = await user_service.get_by_id(user_id)
    by = mask(admin.get("mobile_number") or "")
    await subscription_service.extend(
        user_id=user["_id"], days=days, plan_id=None, plan_name="Granted by admin",
        source=subscription_service.SOURCE_ADMIN, by=by, note=note,
    )
    logger.warning("Admin %s granted %d days to user %s (%s)", by, days, user_id, note or "no note")
    return await user_subscription(user_id)


async def revoke(user_id: str, note: Optional[str], admin: dict) -> UserSubscriptionAdminOut:
    user = await user_service.get_by_id(user_id)
    by = mask(admin.get("mobile_number") or "")
    await subscription_service.end_now(user["_id"], "admin_revoke", by=by, note=note)
    logger.warning("Admin %s revoked access of user %s (%s)", by, user_id, note or "no note")
    return await user_subscription(user_id)
