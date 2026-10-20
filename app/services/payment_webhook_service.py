"""
services/payment_webhook_service.py — Razorpay webhooks.

Why: the app's /verify only runs if the app is still open after paying. The
webhook is Razorpay telling us directly, so a user who closes the app (or loses
network) mid-payment still gets access, and refunds take access back.

Trust: only requests whose X-Razorpay-Signature matches HMAC-SHA256(webhook
secret, raw body) are processed. Anything else is rejected with 400 and never
touches the database.

Idempotency: Razorpay retries until it gets a 2xx and can deliver the same
event twice. Deliveries are de-duplicated by event id, and every action is
itself idempotent (conditional status updates), so replays never grant twice.

Webhooks are processed even while PAYMENTS_ENABLED is off — if money moved,
the user must get what they paid for.

Errors: a signed event we can't act on (unknown order, mismatch) is logged and
answered 200 so Razorpay stops retrying; an unexpected failure raises → 500 so
Razorpay retries later.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.providers.payments import get_payment_provider
from app.repositories import payment_event_repo, payment_repo
from app.services import payment_service, subscription_service

logger = logging.getLogger("nutriblend.payments.webhook")


class InvalidWebhook(Exception):
    """Bad or missing signature, or a body that isn't a Razorpay event."""


def _entity(event: dict, name: str) -> dict:
    return ((event.get("payload") or {}).get(name) or {}).get("entity") or {}


async def handle(raw_body: bytes, signature: Optional[str], event_id_header: Optional[str]) -> str:
    """Process one delivery. Returns the result label (also stored)."""
    if not get_payment_provider().verify_webhook_signature(raw_body, signature):
        logger.warning("Rejected webhook: bad or missing signature")
        raise InvalidWebhook("invalid signature")
    try:
        event: dict[str, Any] = json.loads(raw_body)
    except ValueError as exc:
        raise InvalidWebhook("body is not JSON") from exc

    name = event.get("event") or ""
    # Razorpay always sends X-Razorpay-Event-Id; the body hash is a fallback.
    event_id = event_id_header or "sha256:" + hashlib.sha256(raw_body).hexdigest()
    if await payment_event_repo.seen(event_id):
        logger.info("Webhook %s (%s) already processed", event_id, name)
        return "duplicate"

    payment = _entity(event, "payment")
    refund = _entity(event, "refund")
    order_id = payment.get("order_id") or _entity(event, "order").get("id")
    payment_id = payment.get("id") or refund.get("payment_id")

    if name in ("payment.captured", "order.paid"):
        result = await _on_captured(payment, order_id)
    elif name == "payment.failed":
        result = await _on_failed(payment, order_id)
    elif name == "refund.processed":
        result = await _on_refund(refund, payment)
    else:
        result = "ignored_event"

    await payment_event_repo.record({
        "event_id": event_id,
        "event": name,
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "result": result,
        "received_at": datetime.now(timezone.utc),
    })
    logger.info("Webhook %s %s order=%s payment=%s -> %s", event_id, name, order_id, payment_id, result)
    return result


async def _on_captured(payment: dict, order_id: Optional[str]) -> str:
    if not order_id or not payment.get("id"):
        return "ignored_event"
    order = await payment_repo.find_by_razorpay_order_id(order_id)
    if order is None:
        # Not created through our API (e.g. a payment link) — nothing to grant.
        logger.warning("Webhook for unknown order %s", order_id)
        return "ignored_unknown_order"
    if payment.get("status") != "captured" or not payment_service.payment_matches_order(payment, order):
        logger.error(
            "Webhook payment/order mismatch: order=%s payment=%s status=%s amount=%s currency=%s",
            order_id, payment.get("id"), payment.get("status"), payment.get("amount"), payment.get("currency"),
        )
        return "ignored_mismatch"
    granted = await payment_service.complete_paid_order(order, payment["id"], payment_service.VIA_WEBHOOK)
    return "granted" if granted else "already_done"


async def _on_failed(payment: dict, order_id: Optional[str]) -> str:
    if not order_id:
        return "ignored_event"
    reason = payment.get("error_description") or payment.get("error_reason") or "payment failed"
    await payment_repo.mark_failed_if_created(order_id, reason, datetime.now(timezone.utc))
    return "failed_recorded"


async def _on_refund(refund: dict, payment: dict) -> str:
    payment_id = refund.get("payment_id") or payment.get("id")
    if not payment_id:
        return "ignored_event"
    order = await payment_repo.mark_refunded(payment_id, datetime.now(timezone.utc))
    if order is None:
        return "already_done"
    if refund.get("amount") is not None and refund.get("amount") != order["amount"]:
        # Partial refunds still end this purchase's access — flagged for a human.
        logger.warning("Partial refund %s of %s on order %s: treated as full", refund.get("amount"), order["amount"], order["razorpay_order_id"])
    await subscription_service.take_back_days(order["user_id"], order["duration_days"], order["razorpay_order_id"])
    return "refunded"
