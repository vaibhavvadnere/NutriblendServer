"""
services/payment_service.py — Creating Razorpay orders and confirming payments.

Flow
----
1. App: POST /payments/orders {plan_id}. We look the price up in the plan
   (never trusting the app), create a Razorpay order and store it as "created".
2. App opens Razorpay Checkout; the user pays; Checkout returns
   (order_id, payment_id, signature) to the app.
3. App: POST /payments/verify with those three. We
     a) load OUR order record and check it belongs to this user,
     b) check the HMAC signature against OUR order id,
     c) fetch the payment from Razorpay and check it is captured, for this
        order, for exactly this amount and currency,
     d) flip created → paid atomically and extend the subscription.
4. Razorpay's webhook (S6) does (c)+(d) independently, so a user who closes the
   app mid-payment still gets access. Whichever of verify/webhook arrives first
   wins the conditional update; the other is a no-op — never double access.

Card / UPI details never touch this server (Razorpay Checkout collects them).
"""

import logging
import secrets
from datetime import datetime, timezone

from app.core.config import settings
from app.core.exceptions import (
    OrderNotFound,
    PaymentPending,
    PaymentProviderUnavailable,
    PaymentsDisabled,
    PaymentVerificationFailed,
)
from app.providers.payments import PaymentProviderError, get_payment_provider
from app.repositories import payment_repo
from app.schemas.payment import OrderOut, PlanOut, SubscriptionOut
from app.services import plan_service, rate_limiter, subscription_service
from app.utils.phone import mask

logger = logging.getLogger("nutriblend.payments")

VIA_VERIFY = "verify"
VIA_WEBHOOK = "webhook"


def ensure_enabled() -> None:
    if not settings.PAYMENTS_ENABLED:
        raise PaymentsDisabled()


async def list_plans() -> list[PlanOut]:
    ensure_enabled()
    return await plan_service.list_active()


async def create_order(user: dict, plan_id: str, ip: str) -> OrderOut:
    ensure_enabled()
    await rate_limiter.enforce(*rate_limiter.payment_order_rules(str(user["_id"]), ip))
    plan = await plan_service.get_active(plan_id)
    provider = get_payment_provider()

    currency = plan.get("currency") or settings.PAYMENTS_CURRENCY
    receipt = f"nb_{secrets.token_hex(8)}"
    try:
        order = await provider.create_order(
            amount=plan["amount"],
            currency=currency,
            receipt=receipt,
            # Shown in the Razorpay dashboard — ids only, no personal data.
            notes={"user_id": str(user["_id"]), "plan_id": str(plan["_id"])},
        )
    except PaymentProviderError as exc:
        raise PaymentProviderUnavailable() from exc

    now = datetime.now(timezone.utc)
    await payment_repo.insert({
        "user_id": user["_id"],
        "mobile_number": user.get("mobile_number"),
        "plan_id": str(plan["_id"]),
        "plan_name": plan["name"],
        "duration_days": plan["duration_days"],
        "amount": plan["amount"],
        "currency": currency,
        "receipt": receipt,
        "razorpay_order_id": order["id"],
        "status": payment_repo.STATUS_CREATED,
        "created_at": now,
        "updated_at": now,
    })
    logger.info("Order created: %s user=%s plan=%s amount=%s", order["id"], mask(user.get("mobile_number") or ""), plan["name"], plan["amount"])
    return OrderOut(
        order_id=order["id"],
        amount=plan["amount"],
        currency=currency,
        key_id=provider.key_id,
        plan_id=str(plan["_id"]),
        plan_name=plan["name"],
    )


def payment_matches_order(payment: dict, order: dict) -> bool:
    return (
        payment.get("order_id") == order["razorpay_order_id"]
        and payment.get("amount") == order["amount"]
        and (payment.get("currency") or "").upper() == order["currency"].upper()
    )


async def complete_paid_order(order: dict, payment_id: str, via: str) -> bool:
    """created/failed → paid, then extend the subscription. Returns True only for
    the caller that actually flipped it (so access is granted exactly once)."""
    updated = await payment_repo.mark_paid_if_unpaid(
        order["razorpay_order_id"], payment_id, via, datetime.now(timezone.utc)
    )
    if updated is None:
        return False
    await subscription_service.extend(
        user_id=order["user_id"],
        days=order["duration_days"],
        plan_id=order["plan_id"],
        plan_name=order["plan_name"],
        source=subscription_service.SOURCE_PAYMENT,
        order_id=order["razorpay_order_id"],
    )
    logger.info("Payment confirmed via %s: order=%s payment=%s", via, order["razorpay_order_id"], payment_id)
    return True


async def verify(user: dict, order_id: str, payment_id: str, signature: str) -> SubscriptionOut:
    ensure_enabled()
    order = await payment_repo.find_by_razorpay_order_id(order_id)
    # Unknown and someone-else's look identical, so order ids can't be probed.
    if order is None or order["user_id"] != user["_id"]:
        logger.warning("Verify for unknown/foreign order %s by user=%s", order_id, user["_id"])
        raise OrderNotFound()

    provider = get_payment_provider()
    if not provider.verify_payment_signature(order["razorpay_order_id"], payment_id, signature):
        logger.warning("Bad payment signature: order=%s payment=%s user=%s", order_id, payment_id, user["_id"])
        raise PaymentVerificationFailed()

    if order["status"] in (payment_repo.STATUS_PAID, payment_repo.STATUS_REFUNDED):
        # Already settled (e.g. the webhook was faster). Same payment → idempotent
        # success; a different payment id on a settled order is not accepted.
        if order.get("razorpay_payment_id") != payment_id:
            raise PaymentVerificationFailed()
        return await subscription_service.get_for_user(user["_id"])

    try:
        payment = await provider.fetch_payment(payment_id)
    except PaymentProviderError as exc:
        # Signature was valid, so the money most likely moved; the webhook will
        # finish this. Tell the app to check back rather than calling it a failure.
        raise PaymentPending() from exc

    if not payment_matches_order(payment, order):
        logger.error(
            "Payment/order mismatch: order=%s payment=%s got order_id=%s amount=%s currency=%s",
            order_id, payment_id, payment.get("order_id"), payment.get("amount"), payment.get("currency"),
        )
        raise PaymentVerificationFailed()

    status = payment.get("status")
    if status == "captured":
        await complete_paid_order(order, payment_id, VIA_VERIFY)
    elif status == "authorized":
        # Bank approved, capture still pending — the payment.captured webhook completes it.
        raise PaymentPending()
    else:
        await payment_repo.mark_failed_if_created(order_id, f"payment status {status}", datetime.now(timezone.utc))
        raise PaymentVerificationFailed()

    return await subscription_service.get_for_user(user["_id"])


async def my_subscription(user: dict) -> SubscriptionOut:
    ensure_enabled()
    return await subscription_service.get_for_user(user["_id"])
