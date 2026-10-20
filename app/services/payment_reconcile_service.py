"""
services/payment_reconcile_service.py — Safety net for paid-but-unconfirmed orders.

Normally a payment is confirmed twice over: by the app (/verify) and by
Razorpay's webhook. If BOTH are lost — app closed right after paying and the
webhook delivery failed for good (or wasn't configured yet) — the money is
taken but the user gets nothing. This closes that gap:

* a background loop (started in main.lifespan) runs every
  PAYMENTS_RECONCILE_INTERVAL_MINUTES and asks Razorpay about recent orders
  still "created"/"failed" (between PAYMENTS_RECONCILE_MIN_AGE_MINUTES and
  PAYMENTS_RECONCILE_LOOKBACK_HOURS old), at most PAYMENTS_RECONCILE_BATCH per run;
* admins can check one order on demand from the dashboard.

A captured payment found this way is completed through the same conditional
update as verify/webhook, so it can never grant twice.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.exceptions import OrderNotFound, PaymentProviderUnavailable
from app.providers.payments import PaymentProviderError, get_payment_provider
from app.repositories import payment_repo
from app.schemas.payment import ReconcileResult
from app.services import admin_payment_service, payment_service

logger = logging.getLogger("nutriblend.payments.reconcile")

VIA_RECONCILE = "reconcile"      # the background job
VIA_ADMIN_CHECK = "admin_check"  # the dashboard button

# Result labels
GRANTED = "granted"
ALREADY_DONE = "already_done"
NOT_PAID = "not_paid"
PENDING_CAPTURE = "pending_capture"
MISMATCH = "mismatch"


async def reconcile_order(order: dict, via: str) -> str:
    """Ask Razorpay about one of our orders; complete it if it was paid."""
    if order["status"] not in (payment_repo.STATUS_CREATED, payment_repo.STATUS_FAILED):
        return ALREADY_DONE
    payments = await get_payment_provider().fetch_order_payments(order["razorpay_order_id"])
    await payment_repo.mark_checked(order["razorpay_order_id"], datetime.now(timezone.utc))

    captured = [p for p in payments if p.get("status") == "captured"]
    for payment in captured:
        if payment_service.payment_matches_order(payment, order):
            done = await payment_service.complete_paid_order(order, payment["id"], via)
            if done:
                logger.warning("Reconcile completed a paid order: order=%s payment=%s via=%s",
                               order["razorpay_order_id"], payment["id"], via)
            return GRANTED if done else ALREADY_DONE
    if captured:
        logger.error("Reconcile: captured payment(s) on order %s don't match it (amount/currency)", order["razorpay_order_id"])
        return MISMATCH
    if any(p.get("status") == "authorized" for p in payments):
        return PENDING_CAPTURE
    return NOT_PAID


async def run_once() -> dict[str, int]:
    """One pass over recent unconfirmed orders. Returns counts per result."""
    now = datetime.now(timezone.utc)
    orders = await payment_repo.find_unsettled(
        created_after=now - timedelta(hours=settings.PAYMENTS_RECONCILE_LOOKBACK_HOURS),
        created_before=now - timedelta(minutes=settings.PAYMENTS_RECONCILE_MIN_AGE_MINUTES),
        limit=settings.PAYMENTS_RECONCILE_BATCH,
    )
    counts: dict[str, int] = {}
    for order in orders:
        try:
            result = await reconcile_order(order, VIA_RECONCILE)
        except PaymentProviderError as exc:
            logger.warning("Reconcile: Razorpay unreachable (%s); stopping this run", exc)
            counts["error"] = counts.get("error", 0) + 1
            break
        except Exception:  # one bad order must not stop the others
            logger.exception("Reconcile failed for order %s", order.get("razorpay_order_id"))
            counts["error"] = counts.get("error", 0) + 1
            continue
        counts[result] = counts.get(result, 0) + 1
    if counts.get(GRANTED):
        logger.warning("Reconcile run: %s", counts)
    elif orders:
        logger.info("Reconcile run: %s", counts)
    return counts


def active() -> bool:
    """Run the loop only when it can talk to a real payment provider."""
    return (
        settings.PAYMENTS_RECONCILE_ENABLED
        and settings.PAYMENT_PROVIDER == "razorpay"
        and bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)
    )


async def worker_loop() -> None:
    """Background task: run_once() every PAYMENTS_RECONCILE_INTERVAL_MINUTES.
    Runs even while PAYMENTS_ENABLED is off — paid orders must still be honoured."""
    interval = max(1, settings.PAYMENTS_RECONCILE_INTERVAL_MINUTES) * 60
    logger.info("Payment reconcile loop started (every %d min)", interval // 60)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payment reconcile run crashed; will retry next interval")
        await asyncio.sleep(interval)


async def check_order(razorpay_order_id: str) -> ReconcileResult:
    """Dashboard button: check one order now."""
    order = await payment_repo.find_by_razorpay_order_id(razorpay_order_id)
    if order is None:
        raise OrderNotFound()
    try:
        result = await reconcile_order(order, VIA_ADMIN_CHECK)
    except PaymentProviderError as exc:
        raise PaymentProviderUnavailable() from exc
    updated = await payment_repo.find_by_razorpay_order_id(razorpay_order_id)
    return ReconcileResult(result=result, order=admin_payment_service.to_order_out(updated))
